# Configuration

```python
from typing import Annotated
from inceptools import Secret, Settings, config

class AppSettings(Settings):
    model_config = Settings.configure(env_prefix="SHOP_")

    port: int = 8080
    database_url: Annotated[str, Secret]
    request_timeout: float = 30.0
    cors_origins: list[str] = ["*"]

settings = config.load(AppSettings)    # once, at boot
app.supply(settings)
```

Built on `pydantic-settings`, so everything you already know about field types,
aliases and nested models applies.

## Every problem at once

```
config: 2 problem(s):
  - SHOP_DATABASE_URL is required but not set
  - SHOP_PORT: Input should be a valid integer
```

A misconfiguration costs one restart to fix, not one restart per missing
variable. The list names the **environment variable**, not the Python field.

## Secrets are masked

```python
log.info("\n".join(settings.describe()))
# SHOP_DATABASE_URL=po********db
# SHOP_PORT=8080
```

Mark a field with `Secret`, or name it something that obviously is one —
anything containing `password`, `secret`, `token`, `key`, `dsn`, `credential`,
`private` or `auth` is masked without the marker. The safe thing is the easy
thing: `settings.describe()` is correct, and `log.info(settings)` is the
mistake nobody should have to remember not to make.

## Load it once, into the container

```python
app.supply(settings)

@Injectable
class Repo:
    def __init__(self, settings: AppSettings) -> None: ...
```

A config object resolved lazily on first use is a config object whose absence
is discovered by a request rather than by the deploy.

## `.env`

```python
config.load_dotenv(".env")      # before load()
```

Sets only variables not already in the environment — a real environment
variable always beats a committed file. A missing file is not an error; it is
the normal case in production.

## Reload

`app.reload()` runs every `on_reload` hook. A server wires it to `SIGHUP`; a
serverless control plane calls it directly, because it has no signals. Reload
never fails fast: a bad reload keeps the old configuration running rather than
taking the process down.
