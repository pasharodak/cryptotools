from typing import Any

from ctengine.enums import RunMode


def start_webserver(args: dict[str, Any]) -> None:
    """
    Main entry point for webserver mode
    """
    from ctengine.configuration import setup_utils_configuration
    from ctengine.rpc.api_server import ApiServer

    # Initialize configuration

    config = setup_utils_configuration(args, RunMode.WEBSERVER)
    ApiServer(config, standalone=True)
