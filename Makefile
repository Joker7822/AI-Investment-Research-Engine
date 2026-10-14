.PHONY: dev test route

dev:
	uvicorn app.main:app --reload

test:
	pytest -q

route:
	curl -s -X POST http://localhost:8000/v1/route -H 'content-type: application/json' --data @sample_request.json
