#!/usr/bin/python3
"""Agent-facing CLI. JSON is the default output; stdout contains only the result."""
import argparse
import json
import sys

from client import request

KINDS = ('image', 'screenshot', 'pdf', 'doc', 'audio', 'video')


def parser():
    root = argparse.ArgumentParser(prog='omarchy-recall', description='Search indexed local files by meaning or exact text. Outputs JSON by default; no GUI is opened.', epilog='Exit codes: 0 success (including no hits), 1 service/search error, 2 invalid arguments. Start the service with: systemctl --user start omarchy-recall')
    commands = root.add_subparsers(dest='command', required=True)
    search = commands.add_parser('search', help='Search selected folders; code search uses a separate index')
    search.add_argument('query', nargs='+', help='Natural-language description or exact identifier')
    search.add_argument('--code', action='store_true', help='Search configured code roots (equivalent to code: prefix)')
    search.add_argument('--kind', action='append', choices=KINDS, help='Restrict media/document kinds; repeat for multiple kinds')
    search.add_argument('--limit', type=int, default=10, help='Maximum results, 1–100 (default 10)')
    search.add_argument('--keyword-only', action='store_true', help='Skip semantic inference')
    search.add_argument('--json', action='store_true', help='JSON output (the default)')
    commands.add_parser('status', help='Index counts, selected roots, model download and indexing progress')
    text = commands.add_parser('text', help='Read the full indexed passage for a search hit')
    text.add_argument('segment', type=int, help='segment_id from a hit')
    text.add_argument('--code', action='store_true', help='Read from the code index')
    commands.add_parser('pause', help='Pause background indexing')
    commands.add_parser('resume', help='Resume background indexing')
    commands.add_parser('settings', help='Open graphical settings (explicit GUI action)')
    commands.add_parser('toggle', help='Toggle the graphical search panel')
    skill = commands.add_parser('skill', help='Print the bundled agent skill or install it for coding agents')
    skill.add_subparsers(dest='skill_command').add_parser('install', help='Install in ~/.agents/skills and link Claude Code when available')
    return root


def main(argv=None):
    arguments = parser().parse_args(argv)
    if arguments.command == 'skill':
        from agent_skill import SKILL_TEXT, install
        if arguments.skill_command == 'install':
            print(json.dumps(install()))
        else:
            print(SKILL_TEXT, end='')
        return 0
    if arguments.command == 'toggle':
        import os
        from pathlib import Path
        os.execv(str(Path.home() / '.local/bin/omarchy-recall-shell'), ['omarchy-recall-shell', 'shell', 'toggle', 'ferdous.recall', '{}'])
    payload = {'op': arguments.command}
    if arguments.command == 'search':
        if not 1 <= arguments.limit <= 100:
            parser().error('--limit must be between 1 and 100')
        if arguments.code and arguments.kind:
            parser().error('--code and --kind cannot be combined')
        query = ' '.join(arguments.query).strip()
        if not query:
            parser().error('query must not be empty')
        if len(query) > 4096:
            parser().error('query must be at most 4096 characters')
        if arguments.code and not query.startswith('code:'):
            query = 'code: ' + query
        payload.update(query=query, meaning=not arguments.keyword_only, limit=arguments.limit)
        if arguments.kind:
            payload['kinds'] = arguments.kind
    elif arguments.command == 'text':
        if arguments.segment <= 0:
            parser().error('segment must be positive')
        payload.update(segment=arguments.segment, code=arguments.code)
    elif arguments.command in ('pause', 'resume'):
        payload = {'op': 'pause', 'paused': arguments.command == 'pause'}
    reply = request(payload)
    # Semantic fallback must be visible to automation, even when keyword hits exist.
    if reply.get('encoder_error') and payload.get('meaning'):
        reply['warning'] = 'Semantic encoding failed; results may contain only keyword matches: ' + str(reply['encoder_error'])
    print(json.dumps(reply, ensure_ascii=False))
    return 1 if reply.get('error') else 0


if __name__ == '__main__':
    sys.exit(main())
