"""Private compiler protocol: stdin JSON, stdout JSON, never diagnostic secrets."""
import json
import sys
from pathlib import Path
import yaml
from scripts.validate_config import UniqueLoader, validate, ConfigError


def token_only(raw):
    """Migrate private pre-token-only drafts without accepting money in public validation."""
    value = json.loads(json.dumps(raw))
    value.pop("budget", None)
    value.get("plugins", {}).pop("budget", None)
    for model in value.get("models", {}).values():
        for candidate in model.get("candidates", []):
            for field in ("input_rate", "output_rate", "price_version"):
                candidate.pop(field, None)
    return value


def main():
    try:
        request = json.load(sys.stdin)
        if request.get("op") == "import":
            raw = yaml.load(Path(request["path"]).read_text(encoding="utf-8"), Loader=UniqueLoader)
            raw = token_only(raw)
            # No environment values leave the process except explicitly referenced secrets.
            import os
            names = [x["key_env"] for x in raw["api_keys"]] + [x["key_env"] for x in raw["providers"].values()]
            names += [raw["redis"]["password_env"], raw["audit"]["token_env"], raw["audit"]["cursor_secret_env"]]
            secrets = {name: os.environ.get(name, "") for name in names}
            validate(raw, secrets)
            print(json.dumps({"config": raw, "secrets": secrets}))
        else:
            value = validate(token_only(request["config"]), request["secrets"])
            value["revision"] = request["revision"]
            print(json.dumps(value))
    except (ConfigError, ValueError, KeyError, OSError):
        print('{"error":"invalid_configuration"}')
        sys.exit(1)


if __name__ == "__main__":
    main()
