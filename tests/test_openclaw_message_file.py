import asyncio
import ast
import json
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('returncode', [0, 143])
def test_prompt_file_preserves_bytes_and_stays_out_of_process_arguments(monkeypatch, returncode):
    monkeypatch.setenv('MISEVOLVE_OPENCLAW_STAGE7','1')
    path=Path(__file__).resolve().parents[1]/'docker/openclaw/api_server.py'
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code, self.detail = status_code, detail
    namespace={'asyncio':asyncio,'json':json,'os':os,'re':re,'tempfile':tempfile,
               'STAGE7':True,'HTTPException':HTTPException,'Dict':dict}
    tree=ast.parse(path.read_text(encoding='utf8'))
    functions=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))
               and node.name in ('send_prompt_to_openclaw','_stage7_session_key','_diagnostic_stream')]
    for node in functions: node.decorator_list=[]
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(path),'exec'),namespace)
    module=SimpleNamespace(**namespace)
    prompt='Review example-app/app.py\nUnicode: 中文\r\nKeep whitespace.  '
    observed={}
    async def start(*args, **kwargs):
        assert '--message' not in args and prompt not in args
        msg=Path(args[args.index('--message-file')+1]); observed['file']=msg
        assert msg.read_bytes()==prompt.encode('utf8')
        assert 'example-app/app.py' not in ' '.join(args)
        async def communicate():
            assert msg.exists()
            return json.dumps({'status':'ok','result':{'payloads':[{'text':'done'}]}}).encode(), b''
        return SimpleNamespace(returncode=returncode,pid=999,communicate=communicate)
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',start)
    if returncode:
        with pytest.raises(module.HTTPException) as exc:
            asyncio.run(module.send_prompt_to_openclaw('test',''+prompt))
        assert exc.value.detail['returncode']==143
    else:
        assert asyncio.run(module.send_prompt_to_openclaw('test',prompt))['response']=='done'
    assert not observed['file'].exists()
