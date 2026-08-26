.PHONY: evals test hill-climb

PYTHON ?= python3

evals:
	PYTHONPATH=src $(PYTHON) scripts/run_instagram_evals.py

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

hill-climb: evals test
	git diff --check
