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
    StepType,
    ProductionStepTemplate,
    BuildOrderStep,
)

logger = logging.getLogger('ponderosa_plugin')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_body(request):
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return None


def _step_type_dict(st):
    return {
        'id': st.pk,
        'name': st.name,
        'slug': st.slug,
        'description': st.description,
        'color': st.color,
        'icon': st.icon,
        'station_group': st.station_group,
        'is_automatable': st.is_automatable,
        'sort_order': st.sort_order,
        'active': st.active,
    }


def _station_dict(station):
    current = BuildOrderStep.objects.filter(
        station=station, status='in_progress'
    ).select_related('build', 'step_type').first()
    current_step = None
    if current:
        current_step = {
            'id': current.pk,
            'build_id': current.build_id,
            'build_reference': current.build.reference if current.build else None,
            'name': current.name,
            'step_type': _step_type_dict(current.step_type),
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
        'step_type': _step_type_dict(tmpl.step_type),
        'name': tmpl.name,
        'description': tmpl.description,
        'estimated_duration': duration,
        'station_group': tmpl.station_group,
        'is_automatable': tmpl.is_automatable,
        'metadata': tmpl.metadata,
    }


def _step_dict(step):
    station_info = None
    if step.station:
        station_info = {'id': step.station_id, 'name': step.station.name}
    assigned = None
    if step.assigned_to:
        assigned = {'id': step.assigned_to_id, 'username': step.assigned_to.username}
    return {
        'id': step.pk,
        'sequence': step.sequence,
        'step_type': _step_type_dict(step.step_type),
        'name': step.name,
        'status': step.status,
        'station': station_info,
        'assigned_to': assigned,
        'priority': step.priority,
        'started_at': step.started_at.isoformat() if step.started_at else None,
        'completed_at': step.completed_at.isoformat() if step.completed_at else None,
        'operator_notes': step.operator_notes,
        'template_id': step.template_id,
        'metadata': step.metadata,
        'created_at': step.created_at.isoformat() if step.created_at else None,
        'updated_at': step.updated_at.isoformat() if step.updated_at else None,
    }


def _progress_summary(build_pk):
    steps = BuildOrderStep.objects.filter(build_id=build_pk)
    total = steps.count()
    if total == 0:
        return {'total': 0, 'completed': 0, 'in_progress': 0, 'pending': 0,
                'queued': 0, 'on_hold': 0, 'blocked': 0, 'skipped': 0,
                'percent_complete': 0}
    counts = {}
    for s in steps.values_list('status', flat=True):
        counts[s] = counts.get(s, 0) + 1
    completed = counts.get('completed', 0)
    return {
        'total': total,
        'completed': completed,
        'in_progress': counts.get('in_progress', 0),
        'pending': counts.get('pending', 0),
        'queued': counts.get('queued', 0),
        'on_hold': counts.get('on_hold', 0),
        'blocked': counts.get('blocked', 0),
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
        'step_type': step.step_type.slug,
        'step_type_name': step.step_type.name,
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


def _auto_queue_next_step(build_id, completed_sequence):
    """After a step completes or is skipped, queue the next pending step."""
    next_step = BuildOrderStep.objects.filter(
        build_id=build_id,
        sequence__gt=completed_sequence,
        status='pending',
    ).order_by('sequence').first()
    if next_step:
        next_step.status = 'queued'
        next_step.save()
        _forward_step_event(next_step, 'pending', 'queued')


def _check_auto_complete(build):
    """Auto-complete build if all steps are terminal and setting is enabled."""
    plugin = registry.get_plugin('ponderosa')
    if not plugin or not plugin.get_setting('AUTO_COMPLETE_BUILD_ON_STEPS_DONE'):
        return False

    has_remaining = BuildOrderStep.objects.filter(
        build=build,
        status__in=['pending', 'queued', 'in_progress', 'on_hold', 'blocked'],
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
# StepType endpoints
# ---------------------------------------------------------------------------

@csrf_exempt
def step_type_list_create(request):
    if request.method == 'GET':
        qs = StepType.objects.all()
        active = request.GET.get('active')
        if active is not None:
            qs = qs.filter(active=active.lower() in ('true', '1'))
        return JsonResponse([_step_type_dict(st) for st in qs], safe=False)

    if request.method == 'POST':
        body = _json_body(request)
        if not body:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        name = body.get('name', '').strip()
        slug = body.get('slug', '').strip()
        if not name or not slug:
            return JsonResponse({'error': 'name and slug are required'}, status=400)
        if StepType.objects.filter(slug=slug).exists():
            return JsonResponse({'error': f'Step type with slug "{slug}" already exists'}, status=409)
        st = StepType.objects.create(
            name=name,
            slug=slug,
            description=body.get('description', ''),
            color=body.get('color', '#1971c2'),
            icon=body.get('icon', ''),
            station_group=body.get('station_group', ''),
            is_automatable=body.get('is_automatable', False),
            sort_order=body.get('sort_order', 0),
            active=body.get('active', True),
        )
        return JsonResponse(_step_type_dict(st), status=201)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
def step_type_detail(request, pk):
    try:
        st = StepType.objects.get(pk=pk)
    except StepType.DoesNotExist:
        return JsonResponse({'error': 'Step type not found'}, status=404)

    if request.method == 'GET':
        return JsonResponse(_step_type_dict(st))

    if request.method == 'PUT':
        body = _json_body(request)
        if not body:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        for field in ('name', 'slug', 'description', 'color', 'icon',
                      'station_group', 'is_automatable', 'sort_order', 'active'):
            if field in body:
                setattr(st, field, body[field])
        st.save()
        return JsonResponse(_step_type_dict(st))

    if request.method == 'DELETE':
        has_templates = ProductionStepTemplate.objects.filter(step_type=st).exists()
        has_steps = BuildOrderStep.objects.filter(step_type=st).exists()
        if has_templates or has_steps:
            return JsonResponse(
                {'error': 'Step type is in use by templates or build steps and cannot be deleted'},
                status=409,
            )
        st.delete()
        return JsonResponse({'status': 'deleted'})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


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
        if not name:
            return JsonResponse({'error': 'name is required'}, status=400)
        station = Station.objects.create(
            name=name,
            station_type=body.get('station_type', ''),
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
        status__in=['queued', 'in_progress'],
        build__status__in=[10, 20],
    ).select_related('build', 'step_type').order_by('priority', 'build__target_date', 'sequence')

    queue = []
    for s in steps:
        queue.append({
            'id': s.pk,
            'build_id': s.build_id,
            'build_reference': s.build.reference if s.build else None,
            'sequence': s.sequence,
            'step_type': _step_type_dict(s.step_type),
            'name': s.name,
            'status': s.status,
            'priority': s.priority,
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
        templates = ProductionStepTemplate.objects.filter(part=part).select_related('step_type')
        return JsonResponse([_template_dict(t) for t in templates], safe=False)

    if request.method == 'POST':
        body = _json_body(request)
        if not body:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        step_type_id = body.get('step_type_id')
        if not step_type_id:
            return JsonResponse({'error': 'step_type_id is required'}, status=400)
        try:
            step_type = StepType.objects.get(pk=step_type_id)
        except StepType.DoesNotExist:
            return JsonResponse({'error': 'Invalid step_type_id'}, status=400)
        name = body.get('name', '').strip()
        if not name:
            return JsonResponse({'error': 'name is required'}, status=400)
        max_seq = ProductionStepTemplate.objects.filter(part=part).aggregate(
            m=Max('sequence'))['m'] or 0
        duration = None
        if body.get('estimated_duration'):
            duration = _parse_duration(body['estimated_duration'])
        tmpl = ProductionStepTemplate.objects.create(
            part=part,
            sequence=max_seq + 1,
            step_type=step_type,
            name=name,
            description=body.get('description', ''),
            estimated_duration=duration,
            station_group=body.get('station_group', ''),
            is_automatable=body.get('is_automatable', False),
            metadata=body.get('metadata', {}),
        )
        return JsonResponse(_template_dict(tmpl), status=201)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
def step_template_detail(request, part_pk, pk):
    try:
        tmpl = ProductionStepTemplate.objects.select_related('step_type').get(pk=pk, part_id=part_pk)
    except ProductionStepTemplate.DoesNotExist:
        return JsonResponse({'error': 'Template not found'}, status=404)

    if request.method == 'GET':
        return JsonResponse(_template_dict(tmpl))

    if request.method == 'PUT':
        body = _json_body(request)
        if not body:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        if 'step_type_id' in body:
            try:
                tmpl.step_type = StepType.objects.get(pk=body['step_type_id'])
            except StepType.DoesNotExist:
                return JsonResponse({'error': 'Invalid step_type_id'}, status=400)
        if 'name' in body:
            tmpl.name = body['name'].strip()
        if 'description' in body:
            tmpl.description = body['description']
        if 'estimated_duration' in body:
            tmpl.estimated_duration = _parse_duration(body['estimated_duration'])
        if 'station_group' in body:
            tmpl.station_group = body['station_group']
        if 'is_automatable' in body:
            tmpl.is_automatable = body['is_automatable']
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
    existing = {t.sequence: t for t in ProductionStepTemplate.objects.filter(part=part).select_related('step_type')}

    result = []
    for idx, step_data in enumerate(incoming, start=1):
        step_type_id = step_data.get('step_type_id')
        name = step_data.get('name', '').strip()
        if not step_type_id or not name:
            continue
        try:
            step_type = StepType.objects.get(pk=step_type_id)
        except StepType.DoesNotExist:
            continue
        duration = _parse_duration(step_data.get('estimated_duration'))
        metadata = step_data.get('metadata', {})

        if idx in existing:
            tmpl = existing.pop(idx)
            tmpl.step_type = step_type
            tmpl.name = name
            tmpl.description = step_data.get('description', '')
            tmpl.estimated_duration = duration
            tmpl.station_group = step_data.get('station_group', '')
            tmpl.is_automatable = step_data.get('is_automatable', False)
            tmpl.metadata = metadata
            tmpl.save()
        else:
            tmpl = ProductionStepTemplate.objects.create(
                part=part, sequence=idx, step_type=step_type,
                name=name, description=step_data.get('description', ''),
                estimated_duration=duration,
                station_group=step_data.get('station_group', ''),
                is_automatable=step_data.get('is_automatable', False),
                metadata=metadata,
            )
        result.append(_template_dict(tmpl))

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

    steps = BuildOrderStep.objects.filter(build=build).select_related('station', 'step_type', 'assigned_to')
    return JsonResponse({
        'build_id': build.pk,
        'build_reference': build.reference,
        'progress': _progress_summary(build.pk),
        'steps': [_step_dict(s) for s in steps],
    })


def _get_step(build_pk, pk):
    try:
        return BuildOrderStep.objects.select_related('build', 'station', 'step_type', 'assigned_to').get(
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
    if step.status not in ('pending', 'queued', 'on_hold'):
        return JsonResponse({'error': f'Cannot start step in {step.status} status'}, status=409)

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

    _auto_queue_next_step(step.build_id, step.sequence)

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
def build_step_block(request, build_pk, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    step = _get_step(build_pk, pk)
    if not step:
        return JsonResponse({'error': 'Step not found'}, status=404)
    if step.status not in ('queued', 'in_progress'):
        return JsonResponse({'error': f'Cannot block step in {step.status} status'}, status=409)

    body = _json_body(request)
    if body and body.get('notes'):
        step.operator_notes = body['notes']

    old_status = step.status
    step.status = 'blocked'
    step.save()
    _forward_step_event(step, old_status, 'blocked')
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
    if step.status not in ('pending', 'queued', 'on_hold', 'blocked'):
        return JsonResponse({'error': f'Cannot skip step in {step.status} status'}, status=409)

    old_status = step.status
    step.status = 'skipped'
    step.save()
    _forward_step_event(step, old_status, 'skipped')

    _auto_queue_next_step(step.build_id, step.sequence)

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
# Manager / dispatch views
# ---------------------------------------------------------------------------

@require_GET
def production_unassigned(request):
    steps = BuildOrderStep.objects.filter(
        station__isnull=True,
        status__in=['pending', 'queued', 'in_progress'],
        build__status__in=[10, 20],
    ).select_related('build', 'step_type').order_by('priority', 'build__target_date', 'sequence')

    result = []
    for s in steps:
        result.append({
            'id': s.pk,
            'build_id': s.build_id,
            'build_reference': s.build.reference if s.build else None,
            'sequence': s.sequence,
            'step_type': _step_type_dict(s.step_type),
            'name': s.name,
            'status': s.status,
            'priority': s.priority,
            'build_target_date': s.build.target_date.isoformat() if s.build and s.build.target_date else None,
        })
    return JsonResponse({'steps': result, 'count': len(result)})


@require_GET
def production_on_hold(request):
    steps = BuildOrderStep.objects.filter(
        status__in=['on_hold', 'blocked'],
        build__status__in=[10, 20],
    ).select_related('build', 'station', 'step_type').order_by('build__target_date', 'sequence')

    result = []
    for s in steps:
        result.append({
            'id': s.pk,
            'build_id': s.build_id,
            'build_reference': s.build.reference if s.build else None,
            'sequence': s.sequence,
            'step_type': _step_type_dict(s.step_type),
            'name': s.name,
            'status': s.status,
            'station': {'id': s.station_id, 'name': s.station.name} if s.station else None,
            'operator_notes': s.operator_notes,
            'build_target_date': s.build.target_date.isoformat() if s.build and s.build.target_date else None,
        })
    return JsonResponse({'steps': result, 'count': len(result)})


@require_GET
def production_overview(request):
    from django.db.models import Count

    status_counts = BuildOrderStep.objects.filter(
        build__status__in=[10, 20],
    ).values('status').annotate(count=Count('id'))
    statuses = {s['status']: s['count'] for s in status_counts}

    total_stations = Station.objects.filter(active=True).count()
    busy_stations = Station.objects.filter(
        active=True,
        assigned_steps__status='in_progress',
    ).distinct().count()

    return JsonResponse({
        'steps_by_status': {
            'pending': statuses.get('pending', 0),
            'queued': statuses.get('queued', 0),
            'in_progress': statuses.get('in_progress', 0),
            'completed': statuses.get('completed', 0),
            'on_hold': statuses.get('on_hold', 0),
            'blocked': statuses.get('blocked', 0),
            'skipped': statuses.get('skipped', 0),
        },
        'stations': {
            'total_active': total_stations,
            'busy': busy_stations,
            'available': total_stations - busy_stations,
        },
    })


@csrf_exempt
def dispatch_bulk_assign(request):
    """Bulk-assign stations to steps."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    body = _json_body(request)
    if not body or 'assignments' not in body:
        return JsonResponse({'error': 'Body must contain "assignments" array'}, status=400)

    results = []
    for assignment in body['assignments']:
        step_id = assignment.get('step_id')
        station_id = assignment.get('station_id')
        try:
            step = BuildOrderStep.objects.get(pk=step_id)
        except BuildOrderStep.DoesNotExist:
            results.append({'step_id': step_id, 'status': 'not_found'})
            continue
        if station_id is None:
            step.station = None
        else:
            try:
                station = Station.objects.get(pk=station_id, active=True)
                step.station = station
            except Station.DoesNotExist:
                results.append({'step_id': step_id, 'status': 'station_not_found'})
                continue
        step.save()
        results.append({'step_id': step_id, 'status': 'assigned', 'station_id': station_id})

    return JsonResponse({'results': results})


@csrf_exempt
def dispatch_reorder(request):
    """Set priority based on ordered step ID list."""
    if request.method != 'PUT':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    body = _json_body(request)
    if not body or 'step_ids' not in body:
        return JsonResponse({'error': 'Body must contain "step_ids" array'}, status=400)

    for priority, step_id in enumerate(body['step_ids']):
        BuildOrderStep.objects.filter(pk=step_id).update(priority=priority)

    return JsonResponse({'status': 'reordered', 'count': len(body['step_ids'])})


@require_GET
def dispatch_board(request):
    """Return steps grouped by station for the dispatch kanban board."""
    steps = BuildOrderStep.objects.filter(
        status__in=['pending', 'queued', 'in_progress'],
        build__status__in=[10, 20],
    ).select_related('build', 'station', 'step_type').order_by('priority', 'build__target_date', 'sequence')

    stations = {s.pk: {'station': _station_dict(s), 'steps': []}
                for s in Station.objects.filter(active=True)}
    stations['unassigned'] = {'station': None, 'steps': []}

    for s in steps:
        key = s.station_id if s.station_id and s.station_id in stations else 'unassigned'
        stations[key]['steps'].append({
            'id': s.pk,
            'build_id': s.build_id,
            'build_reference': s.build.reference if s.build else None,
            'sequence': s.sequence,
            'step_type': _step_type_dict(s.step_type),
            'name': s.name,
            'status': s.status,
            'priority': s.priority,
            'build_target_date': s.build.target_date.isoformat() if s.build and s.build.target_date else None,
        })

    return JsonResponse({'columns': list(stations.values())})


@require_GET
def tracker_tree(request):
    """Return SO -> Build Orders -> Steps hierarchy."""
    from order.models import SalesOrder
    from build.models import Build

    so_id = request.GET.get('so_id')

    if so_id:
        try:
            sales_orders = [SalesOrder.objects.get(pk=so_id)]
        except SalesOrder.DoesNotExist:
            return JsonResponse({'error': 'Sales order not found'}, status=404)
    else:
        sales_orders = SalesOrder.objects.filter(
            status__in=[10, 15, 20],
        ).order_by('-creation_date')[:50]

    tree = []
    for so in sales_orders:
        builds_data = []
        builds = Build.objects.filter(sales_order=so).order_by('reference')
        for build in builds:
            steps = BuildOrderStep.objects.filter(
                build=build
            ).select_related('step_type', 'station', 'assigned_to').order_by('sequence')
            builds_data.append({
                'id': build.pk,
                'reference': build.reference,
                'title': build.title,
                'status': build.status,
                'quantity': build.quantity,
                'target_date': build.target_date.isoformat() if build.target_date else None,
                'progress': _progress_summary(build.pk),
                'steps': [_step_dict(s) for s in steps],
            })
        tree.append({
            'sales_order': {
                'id': so.pk,
                'reference': so.reference,
                'customer_name': so.customer.name if so.customer else None,
                'status': so.status,
                'target_date': so.target_date.isoformat() if so.target_date else None,
            },
            'builds': builds_data,
        })

    return JsonResponse({'tree': tree})
