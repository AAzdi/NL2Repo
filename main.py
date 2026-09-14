"""Run Claude Code evaluations from a benchmark configuration."""

import argparse
import json

from logging_config import get_logger
import test_data_service

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.claude_code.json')
    parser.add_argument('--experiment-name', help='Experiment name; overrides config')
    parser.add_argument('--output-dir', help='Output root; overrides config')
    args = parser.parse_args()

    with open(args.config, encoding='utf-8') as config_file:
        conf = json.load(config_file)
    if conf.get('harness', 'claude_code') != 'claude_code':
        parser.error('Only harness=claude_code is supported; use config.claude_code.json')
    for key in ('experiment_name', 'output_dir'):
        value = getattr(args, key)
        if value is not None:
            conf[key] = value

    test_data_service.read_all_test_data()
    from claude_code.runner import start_claude_code

    results = start_claude_code(conf)
    if any(item['status'] != 'completed' or not item.get('evaluation_valid', False)
           for item in results):
        return 1
    logger.info('claude_code has been executed successfully')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
