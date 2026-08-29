from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from app.config import DEFAULT_CONFIG_PATH, FareConfig
from app.main import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="FARE unified YAML launcher")
    parser.add_argument(
        "command",
        choices=("serve", "requirements", "validate-config"),
        help="serve the API, evaluate requirements, or validate one configuration",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="single YAML configuration file",
    )
    args = parser.parse_args()
    config = FareConfig.load(args.config)
    if args.command == "validate-config":
        print(json.dumps(configuration_summary(config), ensure_ascii=False, indent=2))
        return 0
    if args.command == "requirements":
        from app.requirement_runner import run_requirement_source

        return run_requirement_source(config)
    uvicorn.run(
        create_app(config.settings),
        host=config.server.host,
        port=config.server.port,
        access_log=False,
    )
    return 0


def configuration_summary(config: FareConfig) -> dict[str, object]:
    return {
        "status": "valid",
        "config_id": config.config_id,
        "environment": config.environment,
        "config_fingerprint": config.settings.config_fingerprint,
        "config_path": str(config.path),
        "requirement_source_mode": config.requirement_source.mode,
        "network_plan_mode": config.settings.network_plan_client_mode,
        "acl_mode": config.settings.acl_client_mode,
        "acl_decision_mode": config.settings.acl_decision_mode,
        "acl_deterministic_pending_mode": (
            config.settings.acl_deterministic_pending_mode
        ),
        "llm_mode": config.settings.llm_client_mode,
        "policy_directory": str(config.settings.policy_dir),
        "audit_directory": str(config.settings.audit_log_dir),
        "result_file": str(config.requirement_source.output_file),
    }


if __name__ == "__main__":
    raise SystemExit(main())
