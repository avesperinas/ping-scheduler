.PHONY: run-app app-stop app-logs help

help:
	@echo ""
	@echo "  Development:"
	@echo "    make run-app           — Run locally on :8009 (build included, login dev/dev)"
	@echo "    make app-stop          — Stop"
	@echo "    make app-logs          — Follow logs"
	@echo ""

# Dev credentials are supplied here so a clone runs with no .env. A real
# deployment sets PING_AUTH_USER and PING_AUTH_PASSWORD properly.
run-app:
	PING_AUTH_USER=dev PING_AUTH_PASSWORD=dev docker compose up -d --build

app-stop:
	docker compose down

app-logs:
	docker compose logs -f
