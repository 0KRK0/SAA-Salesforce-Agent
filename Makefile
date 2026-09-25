.PHONY: install backend frontend test lint typecheck build up down key

install:
	cd backend && pip install -r requirements-dev.txt
	cd frontend && npm install

backend:
	cd backend && uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

test:
	cd backend && python -m pytest -q

lint:
	cd backend && python -m ruff check .

typecheck:
	cd frontend && npm run typecheck

build:
	cd frontend && npm run build

up:
	docker compose up --build

down:
	docker compose down

key:
	@python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
