# Every target that touches the running service goes through sops, so the key
# is decrypted into one short-lived process and never lands on disk.

SOPS := sops exec-env secrets.sops.yaml

.PHONY: up down restart logs stats key test bench

up:  ## build and start the shim
	$(SOPS) 'docker compose up -d --build'

down:  ## stop it
	docker compose down

restart:  ## pick up config changes from compose.yaml
	$(SOPS) 'docker compose up -d'

logs:  ## follow the log
	docker compose logs -f

stats:  ## how much has been dictated, and what it cost
	@curl -s http://127.0.0.1:8756/stats; echo

key:  ## edit the encrypted key in $$EDITOR
	sops secrets.sops.yaml

test:  ## end-to-end run against the mock, no Soniox key needed
	./tests/e2e.sh

bench:  ## compare transports: make bench AUDIO=/dir (must contain sample.wav)
	@test -n "$(AUDIO)" || { echo "set AUDIO=/path/to/dir containing sample.wav" >&2; exit 1; }
	@test -f "$(AUDIO)/sample.wav" || { echo "no sample.wav in $(AUDIO)" >&2; exit 1; }
	# The runtime image already carries httpx and websockets (via uvicorn[standard]),
	# so the benchmark needs no image of its own.
	docker build -q -t soniox-openai-shim:local . >/dev/null
	$(SOPS) 'docker run --rm -e SONIOX_API_KEY \
	  -v "$(PWD)/tests:/app/tests:ro" -v "$(AUDIO):/audio:ro" \
	  --entrypoint python soniox-openai-shim:local /app/tests/bench.py /audio/sample.wav'
