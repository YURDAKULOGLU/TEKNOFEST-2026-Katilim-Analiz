"""Production composition for the API and source-processing worker roles."""

from katilim_analiz.runtime.composition import create_production_app
from katilim_analiz.runtime.worker import run_worker

__all__ = ["create_production_app", "run_worker"]
