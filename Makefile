.PHONY: test sample eval report pages all clean
test:      ; python -m pytest -q
sample:    ; rm -f data/holdings.db && python -m holdings_lens sample
eval:      ; python -m holdings_lens eval
report:    ; python -m holdings_lens report
pages:     ; python web/build_pages.py
all: test sample eval report pages
clean:     ; rm -rf data web/dist outreach/out .pytest_cache
