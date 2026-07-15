"""Run scenario automation from scenarios.json, logging every action to the console.

Each scenario looks for its template image and taps it when found, at its own
configurable check interval (default 1s). Ctrl+C to stop.

    python run_scenarios.py
    python run_scenarios.py my_config.json
"""
import sys
import time

from automation_engine import Engine


def main():
    cfg = sys.argv[1] if len(sys.argv) > 1 else 'scenarios.json'
    engine = Engine(cfg, logger=print)

    if not engine.scenarios:
        print('No scenarios in %s. Add some with scenario_ui.py, '
              'or edit the file directly.' % cfg)
        return

    engine.start()
    try:
        while engine.is_running():
            time.sleep(0.3)
    except KeyboardInterrupt:
        print('\nstopping...')
        engine.stop()
        engine.join(timeout=3)


if __name__ == '__main__':
    main()
