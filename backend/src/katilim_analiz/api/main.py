"""ASGI entrypoint for the composed API role."""

from katilim_analiz.runtime.composition import create_production_app

app = create_production_app()
