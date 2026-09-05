"""Offline data pipeline for the food & nutrition recommender.

Importable both from the Airflow DAGs (the dags folder is on sys.path inside
the Airflow image) and from the test suite (see tests/conftest.py).

Phase 1 scope: Spoonacular extraction + persisted point-quota tracking +
row validation. The Airflow DAG that wires these into tasks is Phase 3.
"""
