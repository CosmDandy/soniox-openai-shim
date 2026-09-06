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

bench:  ## compare transports on real audio: make bench AUDIO=/path/to/dir
	docker build -q -t soniox-shim:bench -f tests/Dockerfile.bench . >/dev/null
	$(SOPS) 'docker run --rm -e SONIOX_API_KEY \
	  -v "$(PWD)/tests:/app/tests:ro" -v "$(AUDIO):/audio:ro" \
	  --entrypoint python soniox-shim:bench /app/tests/bench.py /audio/sample.wav'
