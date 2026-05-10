import argparse

from .ws_demo import HOST, PORT, run_server


def main():
    parser = argparse.ArgumentParser(
        description="Levanta el demo HTTP + WebSocket incluido en wsbuilder"
    )
    parser.add_argument("--host", default=HOST, help="Host de escucha")
    parser.add_argument("--port", type=int, default=PORT, help="Puerto de escucha")
    args = parser.parse_args()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
