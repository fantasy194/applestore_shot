"""Real local SRP proofs; accepted passwords persist before later Shop failure."""
import base64
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import alipay_qr
import duo_killer
import interaction
from apple_login import AppleStoreLogin, LoginError, password_key, srp
from service import Service


class ProtocolPasswordCheckpointTests(unittest.TestCase):
    def test_accepted_password_is_saved_before_authx_541_but_unknown_auth_is_not(self):
        for auth_type, saved in (("hsa2", True), ("unexpected", False)):
            with self.subTest(auth_type=auth_type), ExitStack() as stack:
                root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
                old={name:getattr(duo_killer,name) for name in ("ROOT","AUTH_PATH","LOG_PATH","PAYMENT_ROOT")}
                for name,value in old.items():stack.enter_context(patch.object(duo_killer,name,value))
                stack.enter_context(patch.object(interaction,"handler",interaction.handler))
                stack.enter_context(patch.object(alipay_qr,"ROOT",alipay_qr.ROOT))
                stack.enter_context(redirect_stdout(io.StringIO()))
                vault=Mock();channel=Mock(account_id="fixture-account",task_id="fixture-task")
                service=Service(channel,root,root,"owner@example.test",vault)
                service.ensure_auth_file();service.password="fixture-password";service.remember=True
                client=AppleStoreLogin(auth_path=service.auth)
                stack.callback(client.session.close)
                salt=b"0123456789abcdef"
                key=password_key(service.password,salt,100,"s2k")
                n,g=srp.get_ng(srp.NG_2048,None,None)
                x=srp.gen_x(hashlib.sha256,salt,service.account,key)
                verifier=srp.long_to_bytes(pow(g,x,n));server=None
                def auth(method,path,**kwargs):
                    nonlocal server
                    payload=kwargs["json"]
                    if path=="/federate":return Mock(status_code=200),{"federated":False}
                    if path=="/signin/init":
                        server=srp.Verifier(service.account,salt,verifier,base64.b64decode(payload["a"]),hash_alg=srp.SHA256)
                        _,challenge=server.get_challenge()
                        return Mock(status_code=200),{"salt":base64.b64encode(salt).decode(),"b":base64.b64encode(challenge).decode(),"iteration":100,"protocol":"s2k","c":"fixture"}
                    self.assertEqual(path,"/signin/complete")
                    self.assertEqual(service._password_submitted,service.password)
                    self.assertEqual(server.verify_session(base64.b64decode(payload["m1"])),base64.b64decode(payload["m2"]))
                    return Mock(status_code=200),{"authType":auth_type}
                def exchange(**kwargs):
                    vault.save.assert_called_once_with(channel.account_id,service.password)
                    raise LoginError("fixture Shop rejected",status=541,phase="store_exchange")
                stack.enter_context(patch.object(client,"bootstrap"))
                stack.enter_context(patch.object(client,"auth",side_effect=auth))
                exchange_call=stack.enter_context(patch.object(client,"finish_store_login",side_effect=exchange))
                stack.enter_context(patch.object(client,"discard_failed_login"))
                with self.assertRaises(LoginError):client.login(service.account,service.password)
                if saved:
                    exchange_call.assert_called_once()
                    service.cleanup_credentials(LoginError("fixture Shop rejected",status=541,phase="store_exchange"))
                    vault.invalidate.assert_not_called()
                else:
                    exchange_call.assert_not_called();vault.save.assert_not_called()
