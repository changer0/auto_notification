import base64
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'email_service')]
from email_service import service, local_tools
import server


class SharedMailTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        p=patch.object(service,'MCP_OAUTH_STATE_FILE',self.root/'oauth.json');p.start();self.addCleanup(p.stop)

    def test_environment_instructions_match_actual_tools_and_common_schema(self):
        mcp=service.instructions('mcp');native=service.instructions('executor')
        self.assertIn('receive_email',mcp['instructions'])
        self.assertNotIn('receive_email',native['instructions'])
        self.assertEqual({t['name'] for t in native['tools']},set(service.EXECUTOR_TOOLS))
        left=next(t for t in mcp['tools'] if t['name']=='send_email')['inputSchema']
        right=next(t for t in native['tools'] if t['name']=='send_email')['inputSchema']
        self.assertEqual(left,right)
        self.assertIn('apple',right['properties']['format']['enum'])
        self.assertIn('request_id',right['required'])
        self.assertNotIn('operation_id',right['properties'])
        self.assertNotEqual(mcp['revision'],native['revision'])
        with patch.object(service.notify,'load_options') as load:
            with self.assertRaises(ValueError):service.execute('receive_email',{},context='executor')
            load.assert_not_called()

    def test_mcp_and_executor_share_delivery_ledger(self):
        args={'request_id':'same-request','subject':'测试','body':'内容','format':'apple'}
        with patch.object(service.notify,'load_options',return_value=([],None,[])), \
                patch.object(service.notify,'build_message',return_value=(object(),[])), \
                patch.object(service.notify,'send_message') as send:
            first=server.execute('send_email',args)
            second=local_tools.dispatch({'tool':'send_email','arguments':args})
            self.assertEqual(first['status'],'submitted');self.assertEqual(second['status'],'submitted')
            self.assertTrue(second['deduplicated']);self.assertEqual(first['submitted_at'],second['submitted_at'])
            send.assert_called_once()
            with self.assertRaises(ValueError):local_tools.dispatch({'tool':'send_email','arguments':{**args,'body':'改变正文'}})

    def test_unknown_is_shared_and_not_replayed_by_other_adapter(self):
        args={'request_id':'unknown-request','subject':'测试','body':'内容'}
        with patch.object(service.notify,'load_options',return_value=([],None,[])), \
                patch.object(service.notify,'build_message',return_value=(object(),[])), \
                patch.object(service.notify,'send_message',side_effect=TimeoutError) as send:
            self.assertEqual(server.execute('send_email',args)['status'],'unknown')
            self.assertEqual(local_tools.dispatch({'tool':'send_email','arguments':args})['status'],'unknown')
            send.assert_called_once()

    def test_attachments_are_bytes_not_arbitrary_server_paths(self):
        data=b'attachment-fixture'
        args={'request_id':'with-file','subject':'测试','body':'内容','attachments':[
            {'filename':'report.txt','data_base64':base64.b64encode(data).decode()}]}
        def build(*a,**kw):
            self.assertEqual(kw['attachments'][0].name,'report.txt')
            self.assertEqual(kw['attachments'][0].read_bytes(),data)
            return object(),[]
        with patch.object(service.notify,'load_options',return_value=([],None,[])), \
                patch.object(service.notify,'build_message',side_effect=build),patch.object(service.notify,'send_message'):
            self.assertEqual(service.execute('send_email',args,context='executor')['status'],'submitted')
        with self.assertRaises(ValueError):service.validate('send_email',{**args,'attachments':[{'filename':'../secret','data_base64':'YQ=='}]})

    def test_socket_identity_environment_and_read_only_discovery(self):
        path=self.root/'tools.sock';listener=local_tools.start_executor_server(path)
        def request(payload):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.connect(str(path));connection.sendall(json.dumps(payload).encode()+b'\n')
                with connection.makefile('rb') as stream:return json.loads(stream.readline())
        try:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(request({'action':'describe'})['environment'],'executor')
            self.assertEqual(request({'tool':'read_email_instructions','arguments':{}})['environment'],'executor')
            self.assertEqual(request({'tool':'read_email_instructions','context':'mcp'})['status'],'failed')
            with patch.object(local_tools,'peer_uid',return_value=os.getuid()+1),patch.object(service,'execute') as execute:
                self.assertEqual(request({'tool':'send_email','arguments':{}})['status'],'failed')
                execute.assert_not_called()
        finally:listener.shutdown();listener.server_close()


if __name__=='__main__':unittest.main()
