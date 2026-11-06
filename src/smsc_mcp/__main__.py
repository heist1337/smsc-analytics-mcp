"""Launch the native MCP transport."""

import argparse
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18092)

    args = parser.parse_args()

    token = os.environ.get("MCP_TOKEN")
    if args.transport == "http" and (not token or len(token) < 32):
        parser.error("HTTP requires MCP_TOKEN with at least 32 characters")
    from .server import create_server

    server = create_server(token=token if args.transport == "http" else None)
    if args.transport == "http":
        server.run(transport="http", host=args.host, port=args.port, path="/mcp/")
    else:
        server.run()


if __name__ == "__main__":
    main()
