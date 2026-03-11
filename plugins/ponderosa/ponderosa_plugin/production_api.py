"""Production Steps API — stations, step templates, build steps, and manager views."""

import json
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from plugin.registry import registry

from django.db.models import Max

from ponderosa_plugin.models import (
    Station,
    ProductionStepTemplate,
    BuildOrderStep,
    OPERATION_TYPES,
)

logger = logging.getLogger('ponderosa_plugin')

VALID_OPERATION_TYPES = {t[0] for t in OPERATION_TYPES}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_body(request):
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return None


def _station_dict(station):
    # Find current in-progress step
    current = BuildOrderStep.objects.filter(
        station=station, status='in_progress'
    ).select_related('build').first()
    current_step = None
    if current:
        current_step = {
            'id': current.pk,
            'build_id': current.build_id,
            'build_reference': current.build.reference if current.build else None,
            'name': current.name,
            'operation_type': current.operation_type,
        }
    return {
        'id': station.pk,
        'name': station.name,
        'station_type': station.station_type,
        'active': station.active,
        'metadata': station.metadata,
        'current_step': current_step,
    }


def _template_dict(tmpl):
    duration = None
    if tmpl.estimated_duration:
        total_secs = int(tmpl.estimated_duration.total_seconds())
        hours, remainder = divmod(total_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return {
        'id': tmpl.pk,
        'part_id': tmpl.part_id,
        'sequence': tmpl.sequence,
        'operation_type': tmpl.operation_type,
        'name': tmpl.name,
        'estimated_duration': duration,
        'metadata': tmpl.metadata,
    }


def _step_dict(step):
    station_info = None
    if step.station:
        station_info = {'id': step.station_id, 'name': step.station.name}
    return {
        'id': step.pk,
        'sequence': step.sequence,
        'operation_type': step.operation_type,
        'name': step.name,
        'status': step.status,
        'station': station_info,
        'started_at': step.started_at.isoformat() if step.started_at else None,
        'completed_at': step.completed_at.isoformat() if step.completed_at else None,
        'operator_notes': step.operator_notes,
        'template_id': step.template_id,
        'metadata': step.metadata,
    }


def _progress_summary(build_pk):
    steps = BuildOrderStep.objects.filter(build_id=build_pk)
    total = steps.count()
    if total == 0:
        return {'total': 0, 'completed': 0, 'in_progress': 0, 'pending': 0,
                'on_hold': 0, 'skipped': 0, 'percent_complete': 0}
    counts = {}
    for s in steps.values_list('status', flat=True):
        counts[s] = counts.get(s, 0) + 1
    completed = counts.get('completed', 0)
    return {
        'total': total,
        'completed': completed,
        'in_progress': counts.get('in_progress', 0),
        'pending': counts.get('pending', 0),
        'on_hold': counts.get('on_hold', 0),
        'skipped': counts.get('skipped', 0),
        'percent_complete': round(completed * 100 / total) if total else 0,
    }


def _forward_step_event(step, old_status, new_status):
    """Forward a step transition event to N8N."""
    from ponderosa_plugin.events import forward_event_to_n8n
    from ponderosa_plugin.models import SyncLedger

    payload = {
        'source': 'inventree',
        'event': 'buildstep.status_changed',
        'build_id': step.build_id,
        'step_id': step.pk,
        'step_name': step.name,
        'sequence': step.sequence,
        'operation_type': step.operation_type,
        'old_status': old_status,
        'new_status': new_status,
        'station_id': step.station_id,
    }
    ledger = SyncLedger.objects.filter(
        inventree_model='Build', inventree_pk=step.build_id
    ).first()
    if ledger:
        payload['core_id'] = str(ledger.core_id)
    forward_event_to_n8n(payload)


def _check_auto_complete(build):
    """Auto-complete build if all steps are terminal and setting is enabled."""
    plugin = registry.get_plugin('ponderosa')
    if not plugin or not plugin.get_setting('AUTO_COMPLETE_BUILD_ON_STEPS_DONE'):
        return False

    has_remaining = BuildOrderStep.objects.filter(
        build=build,
        status__in=['pending', 'in_progress', 'on_hold'],
    ).exists()
    if has_remaining:
        return False

    if build.status == 20:  # PRODUCTION
        build.status = 30  # COMPLETE
        build.save()
        logger.info("Auto-completed Build %s — all production steps done", build.pk)
        return True
    return False


# ---------------------------------------------------------------------------
# Station endpoints
# ---------------------------------------------------------------------------

@csrf_exempt
def station_list_create(request):
    if request.method == 'GET':
        qs = Station.objects.all()
        active = request.GET.get('active')
        if active is not None:
            qs = qs.filter(active=active.lower() in ('true', '1'))
        return JsonResponse([_station_dict(s) for s in qs], safe=False)

    if request.method == 'POST':
        body = _json_body(request)
        if not body:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        name = body.get('name', '').strip()
        station_type = body.get('station_type', '')
        if not name:
            return JsonResponse({'error': 'name is required'}, status=400)
        valid_types = {t[0] for t in Station.STATION_TYPES}
        if station_type not in valid_types:
            return JsonResponse({'error': f'station_type must be one of: {", ".join(sorted(valid_types))}'}, status=400)
        station = Station.objects.create(
            name=name,
            station_type=station_type,
            active=body.get('active', True),
            metadata=body.get('metadata', {}),
        )
        return JsonResponse(_station_dict(station), status=201)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
def station_detail(request, pk):
    try:
        station = Station.objects.get(pk=pk)
    except Station.DoesNotExist:
        return JsonResponse({'error': 'Station not found'}, status=404)

    if request.method == 'GET':
        return JsonResponse(_station_dict(station))

    if request.method == 'PUT':
        body = _json_body(request)
        if not body:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        if 'name' in body:
            station.name = body['name'].strip()
        if 'station_type' in body:
            station.station_type = body['station_type']
        if 'active' in body:
            station.active = body['active']
        if 'metadata' in body:
            station.metadata = body['metadata']
        station.save()
        return JsonResponse(_station_dict(station))

    if request.method == 'DELETE':
        has_active = BuildOrderStep.objects.filter(
            station=station, status='in_progress'
        ).exists()
        if has_active:
            return JsonResponse({'error': 'Station has in-progress steps'}, status=409)
        station.delete()
        return JsonResponse({'status': 'deleted'})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@require_GET
def station_queue(request, pk):
    try:
        station = Station.objects.get(pk=pk)
    except Station.DoesNotExist:
        return JsonResponse({'error': 'Station not found'}, status=404)

    steps = BuildOrderStep.objects.filter(
        station=station,
        status__in=['pending', 'in_progress'],
        build__status__in=[10, 20],  # PENDING or PRODUCTION
    ).select_related('build').order_by('build__target_date', 'sequence')

    queue = []
    for s in steps:
        queue.append({
            'id': s.pk,
            'build_id': s.build_id,
            'build_reference': s.build.reference if s.build else None,
            'sequence': s.sequence,
            'operation_type': s.operation_type,
            'name': s.name,
            'status': s.status,
            'started_at': s.started_at.isoformat() if s.started_at else None,
            'build_target_date': s.build.target_date.isoformat() if s.build and s.build.target_date else None,
        })

    return JsonResponse({
        'station': _station_dict(station),
        'queue': queue,
    })


# ---------------------------------------------------------------------------
# Step Template endpoints (Part routing)
# ---------------------------------------------------------------------------

@csrf_exempt
def step_template_list_create(request, part_pk):
    from part.models import Part
    try:
        part = Part.objects.get(pk=part_pk)
    except Part.DoesNotExist:
        return JsonResponse({'error': 'Part not found'}, status=404)

    if request.method == 'GET':
        templates = ProductionStepTemplate.objects.filter(part=part)
        return JsonResponse([_template_dict(t) for t in templates], safe=False)

    if request.method == 'POST':
        body = _json_body(request)
        if not body:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        op_type = body.get('operation_type', '')
        if op_type not in VALID_OPERATION_TYPES:
            return JsonResponse({'error': f'Invalid operation_type'}, status=400)
        name = body.get('name', '').strip()
        if not name:
            return JsonResponse({'error': 'name is required'}, status=400)
        # Auto-assign next sequence number
        max_seq = ProductionStepTemplate.objects.filter(part=part).aggregate(
            m=Max('sequence'))['m'] or 0
        duration = None
        if body.get('estimated_duration'):
            duration = _parse_duration(body['estimated_duration'])
        tmpl = ProductionStepTemplate.objects.create(
            part=part,
            sequence=max_seq + 1,
            operation_type=op_type,
            name=name,
            estimated_duration=duration,
            metadata=body.get('metadata', {}),
        )
        return JsonResponse(_template_dict(tmpl), status=201)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
def step_template_detail(request, part_pk, pk):
    try:
        tmpl = ProductionStepTemplate.objects.get(pk=pk, part_id=part_pk)
    except ProductionStepTemplate.DoesNotExist:
        return JsonResponse({'error': 'Template not found'}, status=404)

    if request.method == 'GET':
        return JsonResponse(_template_dict(tmpl))

    if request.method == 'PUT':
        body = _json_body(request)
        if not body:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        if 'operation_type' in body:
            if body['operation_type'] not in VALID_OPERATION_TYPES:
                return JsonResponse({'error': 'Invalid operation_type'}, status=400)
            tmpl.operation_type = body['operation_type']
        if 'name' in body:
            tmpl.name = body['name'].strip()
        if 'estimated_duration' in body:
            tmpl.estimated_duration = _parse_duration(body['estimated_duration'])
        if 'metadata' in body:
            tmpl.metadata = body['metadata']
        tmpl.save()
        return JsonResponse(_template_dict(tmpl))

    if request.method == 'DELETE':
        tmpl.delete()
        return JsonResponse({'status': 'deleted'})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
def step_template_bulk_sync(request, part_pk):
    """Accept a full ordered list of steps, diff against existing, sync."""
    from part.models import Part
    if request.method != 'PUT':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        part = Part.objects.get(pk=part_pk)
    except Part.DoesNotExist:
        return JsonResponse({'error': 'Part not found'}, status=404)

    body = _json_body(request)
    if not body or 'steps' not in body:
        return JsonResponse({'error': 'Body must contain "steps" array'}, status=400)

    incoming = body['steps']
    existing = {t.sequence: t for t in ProductionStepTemplate.objects.filter(part=part)}

    result = []
    for idx, step_data in enumerate(incoming, start=1):
        op_type = step_data.get('operation_type', '')
        name = step_data.get('name', '').strip()
        if op_type not in VALID_OPERATION_TYPES or not name:
            continue
        duration = _parse_duration(step_data.get('estimated_duration'))
        metadata = step_data.get('metadata', {})

        if idx in existing:
            tmpl = existing.pop(idx)
            tmpl.operation_type = op_type
            tmpl.name = name
            tmpl.estimated_duration = duration
            tmpl.metadata = metadata
            tmpl.save()
        else:
            tmpl = ProductionStepTemplate.objects.create(
                part=part, sequence=idx, operation_type=op_type,
                name=name, estimated_duration=duration, metadata=metadata,
            )
        result.append(_template_dict(tmpl))

    # Delete templates beyond the new list
    for tmpl in existing.values():
        tmpl.delete()

    return JsonResponse(result, safe=False)


def _parse_duration(value):
    """Parse a duration string like '00:30:00' or seconds int into timedelta."""
    import datetime
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.timedelta(seconds=int(value))
    if isinstance(value, str) and ':' in value:
        parts = value.split(':')
        try:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
            return datetime.timedelta(hours=h, minutes=m, seconds=s)
        except (ValueError, IndexError):
            return None
    return None



# ---------------------------------------------------------------------------
# Build Order Step endpoints
# ---------------------------------------------------------------------------

@require_GET
def build_steps_list(request, build_pk):
    from build.models import Build
    try:
        build = Build.objects.get(pk=build_pk)
    except Build.DoesNotExist:
        return JsonResponse({'error': 'Build not found'}, status=404)

    steps = BuildOrderStep.objects.filter(build=build).select_related('station')
    return JsonResponse({
        'build_id': build.pk,
        'build_reference': build.reference,
        'progress': _progress_summary(build.pk),
        'steps': [_step_dict(s) for s in steps],
    })


def _get_step(build_pk, pk):
    try:
        return BuildOrderStep.objects.select_related('build', 'station').get(
            pk=pk, build_id=build_pk)
    except BuildOrderStep.DoesNotExist:
        return None


@csrf_exempt
def build_step_start(request, build_pk, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    step = _get_step(build_pk, pk)
    if not step:
        return JsonResponse({'error': 'Step not found'}, status=404)
    if step.status not in ('pending', 'on_hold'):
        return JsonResponse({'error': f'Cannot start step in {step.status} status'}, status=409)

    # Check station not occupied
    if step.station_id:
        occupied = BuildOrderStep.objects.filter(
            station_id=step.station_id, status='in_progress'
        ).exclude(pk=step.pk).exists()
        if occupied:
            return JsonResponse({'error': 'Station is occupied by another in-progress step'}, status=409)

    old_status = step.status
    step.status = 'in_progress'
    step.started_at = timezone.now()
    step.save()
    _forward_step_event(step, old_status, 'in_progress')
    return JsonResponse({
        'step': _step_dict(step),
        'progress': _progress_summary(build_pk),
        'build_auto_completed': False,
    })


@csrf_exempt
def build_step_complete(request, build_pk, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    step = _get_step(build_pk, pk)
    if not step:
        return JsonResponse({'error': 'Step not found'}, status=404)
    if step.status != 'in_progress':
        return JsonResponse({'error': f'Cannot complete step in {step.status} status'}, status=409)

    old_status = step.status
    step.status = 'completed'
    step.completed_at = timezone.now()
    step.save()
    _forward_step_event(step, old_status, 'completed')

    auto_completed = _check_auto_complete(step.build)
    return JsonResponse({
        'step': _step_dict(step),
        'progress': _progress_summary(build_pk),
        'build_auto_completed': auto_completed,
    })


@csrf_exempt
def build_step_hold(request, build_pk, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    step = _get_step(build_pk, pk)
    if not step:
        return JsonResponse({'error': 'Step not found'}, status=404)
    if step.status != 'in_progress':
        return JsonResponse({'error': f'Cannot hold step in {step.status} status'}, status=409)

    old_status = step.status
    step.status = 'on_hold'
    step.save()
    _forward_step_event(step, old_status, 'on_hold')
    return JsonResponse({
        'step': _step_dict(step),
        'progress': _progress_summary(build_pk),
        'build_auto_completed': False,
    })


@csrf_exempt
def build_step_skip(request, build_pk, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    step = _get_step(build_pk, pk)
    if not step:
        return JsonResponse({'error': 'Step not found'}, status=404)
    if step.status not in ('pending', 'on_hold'):
        return JsonResponse({'error': f'Cannot skip step in {step.status} status'}, status=409)

    old_status = step.status
    step.status = 'skipped'
    step.save()
    _forward_step_event(step, old_status, 'skipped')

    auto_completed = _check_auto_complete(step.build)
    return JsonResponse({
        'step': _step_dict(step),
        'progress': _progress_summary(build_pk),
        'build_auto_completed': auto_completed,
    })


@csrf_exempt
def build_step_assign_station(request, build_pk, pk):
    if request.method != 'PUT':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    step = _get_step(build_pk, pk)
    if not step:
        return JsonResponse({'error': 'Step not found'}, status=404)

    body = _json_body(request)
    if body is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    station_id = body.get('station_id')
    if station_id is None:
        step.station = None
        step.save()
        return JsonResponse({'step': _step_dict(step)})

    try:
        station = Station.objects.get(pk=station_id)
    except Station.DoesNotExist:
        return JsonResponse({'error': 'Station not found'}, status=404)

    if not station.active:
        return JsonResponse({'error': 'Station is not active'}, status=409)

    # Check not occupied if step is in_progress
    if step.status == 'in_progress':
        occupied = BuildOrderStep.objects.filter(
            station=station, status='in_progress'
        ).exclude(pk=step.pk).exists()
        if occupied:
            return JsonResponse({'error': 'Station is occupied by another in-progress step'}, status=409)

    step.station = station
    step.save()
    return JsonResponse({'step': _step_dict(step)})


@csrf_exempt
def build_step_notes(request, build_pk, pk):
    if request.method != 'PUT':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    step = _get_step(build_pk, pk)
    if not step:
        return JsonResponse({'error': 'Step not found'}, status=404)

    body = _json_body(request)
    if body is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    step.operator_notes = body.get('notes', '')
    step.save()
    return JsonResponse({'step': _step_dict(step)})


# ---------------------------------------------------------------------------
# Manager views
# ---------------------------------------------------------------------------

@require_GET
def production_unassigned(request):
    steps = BuildOrderStep.objects.filter(
        station__isnull=True,
        status__in=['pending', 'in_progress'],
        build__status__in=[10, 20],
    ).select_related('build').order_by('build__target_date', 'sequence')

    result = []
    for s in steps:
        result.append({
            'id': s.pk,
            'build_id': s.build_id,
            'build_reference': s.build.reference if s.build else None,
            'sequence': s.sequence,
            'operation_type': s.operation_type,
            'name': s.name,
            'status': s.status,
            'build_target_date': s.build.target_date.isoformat() if s.build and s.build.target_date else None,
        })
    return JsonResponse({'steps': result, 'count': len(result)})


@require_GET
def production_on_hold(request):
    steps = BuildOrderStep.objects.filter(
        status='on_hold',
        build__status__in=[10, 20],
    ).select_related('build', 'station').order_by('build__target_date', 'sequence')

    result = []
    for s in steps:
        result.append({
            'id': s.pk,
            'build_id': s.build_id,
            'build_reference': s.build.reference if s.build else None,
            'sequence': s.sequence,
            'operation_type': s.operation_type,
            'name': s.name,
            'station': {'id': s.station_id, 'name': s.station.name} if s.station else None,
            'operator_notes': s.operator_notes,
            'build_target_date': s.build.target_date.isoformat() if s.build and s.build.target_date else None,
        })
    return JsonResponse({'steps': result, 'count': len(result)})


@require_GET
def production_overview(request):
    from django.db.models import Count, Q

    # Step counts by status across active builds
    status_counts = BuildOrderStep.objects.filter(
        build__status__in=[10, 20],
    ).values('status').annotate(count=Count('id'))
    statuses = {s['status']: s['count'] for s in status_counts}

    # Station utilization
    total_stations = Station.objects.filter(active=True).count()
    busy_stations = Station.objects.filter(
        active=True,
        assigned_steps__status='in_progress',
    ).distinct().count()

    return JsonResponse({
        'steps_by_status': {
            'pending': statuses.get('pending', 0),
            'in_progress': statuses.get('in_progress', 0),
            'completed': statuses.get('completed', 0),
            'on_hold': statuses.get('on_hold', 0),
            'skipped': statuses.get('skipped', 0),
        },
        'stations': {
            'total_active': total_stations,
            'busy': busy_stations,
            'available': total_stations - busy_stations,
        },
    })
