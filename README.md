# Шаблон для проектов с DevPod

Этот репозиторий является отправной точкой для проектов при работе с которыми я использую [DevPod](https://devpod.sh/)

```bash
devpod up git@github.com:CosmDandy/template-devpod.git --id template-devpod-[...] --provider [...]
```

## [Docker in docker](https://github.com/devcontainers/features/tree/main/src/docker-in-docker)

```
  "features": {
    "ghcr.io/devcontainers/features/docker-in-docker:2": {}
  },
```

## [Docker outside of docker](https://github.com/devcontainers/features/tree/main/src/docker-outside-of-docker)

```
  "mounts": [
    "source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"
  ],
  "features": {
    "ghcr.io/devcontainers/features/docker-outside-of-docker:1": {}
  },
  "runArgs": [
    "--privileged",
    "--pid=host",
    "--network=host"
  ],
```

## Секреты: sops + direnv

В корне лежит `.envrc` с функцией `use_sops`. Она расшифровывает
`secrets.sops.yaml` и раскладывает пары в переменные окружения — по одному
обращению к хранилищу на файл, а не на каждое значение.

```bash
# 1. правила шифрования: кому доступен файл
cat > .sops.yaml <<'YAML'
creation_rules:
  - path_regex: secrets\.sops\.yaml$
    age: age1...          # публичный ключ получателя
YAML

# 2. завести секреты (откроется редактор)
sops secrets.sops.yaml

# 3. разрешить direnv — один раз на каталог и после каждой правки .envrc
direnv allow
```

Дальше `cd` в каталог сам поднимает окружение, а `watch_file` перечитает его,
когда `secrets.sops.yaml` изменится.

Зашифрованный файл **коммитится** — без ключа он бесполезен, и в `.gitignore`
его нет намеренно.

### Несколько окружений в одном репозитории

Вложенный `.envrc` подключает функции из корневого первой строкой:

```bash
# terraform/live/prod/.envrc
source_up
use_sops
```

### Функции определяются в репозитории, а не в личном direnvrc

`~/.config/direnv/direnvrc` есть только на твоих машинах, а `.envrc` уезжают в
git. Если объявить `use_sops` там, у всех остальных direnv оборвётся на
`command not found` и не экспортирует ничего — а `terraform` после этого не
упадёт, а пойдёт без `TF_VAR_*`. Поэтому определения живут здесь и опираются
только на штатную stdlib direnv (`has`, `log_error`, `direnv_load`,
`watch_file`).
