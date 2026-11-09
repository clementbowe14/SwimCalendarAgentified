#!/usr/bin/env python3
"""Interactive administration for pilot provider accounts.

Passwords are intentionally accepted only through a non-echoing prompt. Never
add a password command-line option: process arguments are routinely logged.
"""

import argparse
import getpass
import sys
from collections.abc import Sequence

from sqlalchemy.exc import SQLAlchemyError

from app.auth import (
    ProviderAuthAdminError,
    disable_provider_account,
    enable_provider_account,
    provision_provider_account,
    reset_provider_password,
    revoke_all_provider_sessions,
)
from app.config import application_settings_from_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage pilot provider authentication.")
    commands = parser.add_subparsers(dest="command", required=True)

    provision = commands.add_parser("provision", help="Create one provider account.")
    provision.add_argument("--instructor-id", required=True)
    provision.add_argument("--email", required=True)

    reset = commands.add_parser("reset-password", help="Replace a provider password.")
    reset.add_argument("--email", required=True)

    revoke = commands.add_parser("revoke-sessions", help="Revoke every provider session.")
    revoke.add_argument("--email", required=True)

    disable = commands.add_parser("disable", help="Disable a provider account.")
    disable.add_argument("--email", required=True)

    enable = commands.add_parser("enable", help="Re-enable a provider account.")
    enable.add_argument("--email", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        application_settings_from_env()
        if args.command == "provision":
            password = _read_new_password()
            account_id = provision_provider_account(args.instructor_id, args.email, password)
            print(f"Provisioned provider account {account_id}.")
        elif args.command == "reset-password":
            password = _read_new_password()
            reset_provider_password(args.email, password)
            print("Provider password reset and existing sessions revoked.")
        elif args.command == "revoke-sessions":
            revoke_all_provider_sessions(args.email)
            print("Provider sessions revoked.")
        elif args.command == "disable":
            disable_provider_account(args.email)
            print("Provider account disabled and existing sessions revoked.")
        elif args.command == "enable":
            enable_provider_account(args.email)
            print("Provider account enabled; prior sessions remain invalid.")
        else:  # pragma: no cover - argparse enforces the command choices.
            raise AssertionError("Unsupported command")
        return 0
    except ProviderAuthAdminError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print("Database operation failed.", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


def _read_new_password() -> str:
    password = getpass.getpass("New provider password: ")
    confirmation = getpass.getpass("Repeat provider password: ")
    if password != confirmation:
        raise ProviderAuthAdminError("Passwords did not match.")
    return password


if __name__ == "__main__":
    raise SystemExit(main())
