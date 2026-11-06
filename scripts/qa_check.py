"""Run: python scripts/qa_check.py --analysis ... --script ... --narration ..."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa import check_report


def main(argv=None):
    parser = argparse.ArgumentParser(description='분석·대본·나레이션 QA JSON 리포트')
    parser.add_argument('--analysis', required=True, type=Path)
    parser.add_argument('--script', required=True, type=Path)
    parser.add_argument('--narration', required=True, type=Path)
    parser.add_argument('--audio', type=Path, help='검사할 PCM WAV (가능하면 최종 렌더 믹스)')
    parser.add_argument('--output', type=Path, help='리포트를 저장할 JSON 경로')
    args = parser.parse_args(argv)
    try:
        read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
        report = check_report(read(args.analysis), read(args.script), read(args.narration), args.audio)
    except (OSError, ValueError, TypeError) as exc:
        report = {'passed': False, 'checks': [], 'issues': [
            {'code': 'input_invalid', 'severity': 'error', 'message': str(exc)}]}
    value = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(value, encoding='utf-8')
    try:
        print(value)
    except UnicodeEncodeError:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
