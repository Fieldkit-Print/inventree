"""Standalone page views served by the plugin."""

from django.shortcuts import render


def station_queue_page(request):
    return render(request, 'ponderosa/station_queue.html')


def build_tracker_page(request):
    return render(request, 'ponderosa/build_tracker.html')


def dispatch_page(request):
    return render(request, 'ponderosa/dispatch.html')
