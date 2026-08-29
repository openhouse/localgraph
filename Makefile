.PHONY: evals test hill-climb

PYTHON ?= python3

evals:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) scripts/run_instagram_evals.py
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) scripts/run_facebook_evals.py

test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

hill-climb: evals test
	git diff --check
