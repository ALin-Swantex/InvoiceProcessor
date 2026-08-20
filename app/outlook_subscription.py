from __future__ import annotations

import argparse
import json
import os

from app.environment import load_project_environment
from app.outlook_graph import graph_client_from_environment


load_project_environment()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create or renew the Outlook Inbox Graph subscription."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("create")
    renew = subcommands.add_parser("renew")
    renew.add_argument("subscription_id")
    args = parser.parse_args()

    client = graph_client_from_environment()
    if args.command == "create":
        result = client.create_inbox_subscription(
            notification_url=os.environ.get("OUTLOOK_WEBHOOK_URL", ""),
            lifecycle_notification_url=(
                os.environ.get("OUTLOOK_LIFECYCLE_URL") or None
            ),
            client_state=os.environ.get("OUTLOOK_WEBHOOK_CLIENT_STATE", ""),
        )
    else:
        result = client.renew_subscription(args.subscription_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
