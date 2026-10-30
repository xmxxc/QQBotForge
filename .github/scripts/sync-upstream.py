"""Propose an upstream PR without write access to repository contents."""

import json
import os
from urllib.request import Request, urlopen

REPOSITORY = 'xmxxc/QQBotForge'


def github_api(method, path, body=None):
    request = Request(
        'https://api.github.com' + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'},
        method=method,
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def propose(api, run_url):
    prefix = f'/repos/{REPOSITORY}'
    comparison = api('GET', prefix + '/compare/main...ElainaCore:main?per_page=1')
    if comparison['ahead_by'] == 0:
        return {}
    existing = api('GET', prefix + '/pulls?state=open&base=main&head=ElainaCore%3Amain')
    if existing:
        pr = existing[0]
    else:
        pr = api(
            'POST',
            prefix + '/pulls',
            {
                'title': '同步 ElainaCore/ElainaBot_v2 main',
                'head': 'ElainaCore:main',
                'base': 'main',
                'body': '每周上游同步候选，由主人审查并决定是否合并。\n\n'
                '上游分支：ElainaCore/ElainaBot_v2 main；精确 SHA 由 PR head 与验证 job 记录。\n\n'
                f'合并树 CI 将在[本次工作流]({run_url})的独立只读 job 运行并回写结果。'
                'GITHUB_TOKEN 创建的 PR 不会自动触发普通 pull_request CI；此结果不会冒充必需检查。\n\n'
                'Refs #100\nRefs #59\n\n🤖 Generated with [Codex](https://openai.com/codex/)\n',
            },
        )
    return {'pr_number': pr['number'], 'upstream_sha': pr['head']['sha'], 'base_sha': pr['base']['sha']}


def main():
    if os.environ['GITHUB_REPOSITORY'] != REPOSITORY:
        raise RuntimeError('只允许在 xmxxc/QQBotForge 运行上游同步')
    outputs = propose(github_api, f'https://github.com/{REPOSITORY}/actions/runs/{os.environ["GITHUB_RUN_ID"]}')
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        for name, value in outputs.items():
            output.write(f'{name}={value}\n')


if __name__ == '__main__':
    main()
