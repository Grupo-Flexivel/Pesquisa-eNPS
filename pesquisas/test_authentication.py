import uuid
from unittest.mock import patch

from django.contrib.auth import authenticate
from django.test import RequestFactory, TestCase

from .authentication import AuthenticationOutcome, AuthenticationService
from .directory import DirectoryClient, DirectoryEntry, DirectoryResult, guid_from_raw
from .forms import ColaboradorForm
from .models import Empresa, PerfilAcesso, User
from .test_utils import criar_usuario


class HybridAuthenticationTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().post('/accounts/login/')

    @patch('pesquisas.authentication.get_directory_client')
    def test_usuario_local_nao_consulta_ad(self, get_client):
        user = User.objects.create_user(' Joao.Silva ', password='SenhaLocal!2026')

        authenticated = authenticate(
            self.request, username='JOAO.SILVA', password='SenhaLocal!2026'
        )

        self.assertEqual(authenticated.pk, user.pk)
        get_client.assert_not_called()

    @patch('pesquisas.authentication.get_directory_client')
    def test_usuario_ad_vincula_guid_no_primeiro_login(self, get_client):
        guid = uuid.uuid4()
        user = User.objects.create(username='maria.souza', auth_source=User.AuthSource.DIRECTORY)
        user.set_unusable_password()
        user.save()
        get_client.return_value.find_user.return_value = DirectoryEntry('CN=Maria', guid)
        get_client.return_value.verify_credentials.return_value = DirectoryResult.OK

        authenticated = authenticate(self.request, username=' MARIA.SOUZA ', password='SenhaAD!2026')

        self.assertEqual(authenticated.pk, user.pk)
        user.refresh_from_db()
        self.assertEqual(user.directory_guid, guid)
        self.assertEqual(user.failed_login_attempts, 0)

    @patch('pesquisas.authentication.get_directory_client')
    def test_ad_indisponivel_nao_conta_tentativa(self, get_client):
        user = User.objects.create(username='maria.souza', auth_source=User.AuthSource.DIRECTORY)
        user.set_unusable_password()
        user.save()
        get_client.return_value.find_user.return_value = DirectoryResult.UNAVAILABLE

        authenticated = authenticate(self.request, username='maria.souza', password='x')

        self.assertIsNone(authenticated)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(user.failed_login_attempts, 0)
        self.assertEqual(self.request.auth_outcome, AuthenticationOutcome.DIRECTORY_UNAVAILABLE)
        get_client.return_value.verify_credentials.assert_not_called()

    @patch('pesquisas.authentication.get_directory_client')
    def test_guid_divergente_nega_sem_validar_senha(self, get_client):
        stored_guid = uuid.uuid4()
        user = User.objects.create(
            username='maria.souza',
            auth_source=User.AuthSource.DIRECTORY,
            directory_guid=stored_guid,
        )
        user.set_unusable_password()
        user.save()
        get_client.return_value.find_user.return_value = DirectoryEntry('CN=Maria', uuid.uuid4())

        authenticated = authenticate(self.request, username='maria.souza', password='x')

        self.assertIsNone(authenticated)
        self.assertEqual(self.request.auth_outcome, AuthenticationOutcome.IDENTITY_MISMATCH)
        get_client.return_value.verify_credentials.assert_not_called()

    @patch('pesquisas.authentication.get_directory_client')
    def test_senha_vazia_nao_fala_com_ad(self, get_client):
        User.objects.create(username='maria.souza', auth_source=User.AuthSource.DIRECTORY)

        self.assertIsNone(authenticate(self.request, username='maria.souza', password=''))
        get_client.assert_not_called()

    @patch('pesquisas.authentication.get_directory_client')
    def test_credencial_ad_invalida_nao_altera_contador_local(self, get_client):
        user = User.objects.create(username='maria.souza', auth_source=User.AuthSource.DIRECTORY)
        user.set_unusable_password()
        user.save()
        get_client.return_value.find_user.return_value = DirectoryEntry('CN=Maria', uuid.uuid4())
        get_client.return_value.verify_credentials.return_value = DirectoryResult.INVALID

        self.assertIsNone(authenticate(self.request, username='maria.souza', password='errada'))
        user.refresh_from_db()
        self.assertEqual(user.failed_login_attempts, 0)
        self.assertTrue(user.is_active)

    def test_guid_ad_converte_bytes_e_string(self):
        guid = uuid.uuid4()
        self.assertEqual(guid_from_raw(guid.bytes_le), guid)
        self.assertEqual(guid_from_raw(guid.bytes_le.decode('latin-1')), guid)

    def test_busca_usa_samaccountname_com_escape_ldap(self):
        client = DirectoryClient.__new__(DirectoryClient)

        filtro = client._filter_for('joao*(x)\\\x00')

        self.assertEqual(
            filtro,
            r'(&(objectCategory=person)(objectClass=user)(sAMAccountName=joao\2a\28x\29\5c\00))',
        )

    def test_senha_local_invalida_conta_tentativa(self):
        user = User.objects.create_user('local.user', password='correta')
        result = AuthenticationService().authenticate('LOCAL.USER', 'errada', self.request)

        self.assertEqual(result.outcome, AuthenticationOutcome.INVALID)
        user.refresh_from_db()
        self.assertEqual(user.failed_login_attempts, 1)

    def test_usuario_do_cadastro_eh_diretorio(self):
        empresa = Empresa.objects.create(nome='Empresa AD')
        admin = criar_usuario('admin.local', administrador=True)
        perfil = PerfilAcesso.objects.create(nome='Gestor de RH', modulos_permitidos=['enps', 'desligamentos'])
        form = ColaboradorForm(usuario_logado=admin, data={
            'nome': 'João Silva',
            'documento': '52998224725',
            'empresa': empresa.pk,
            'ativo': True,
            'perfil': str(perfil.pk),
            'username': ' Joao.Silva ',
        })

        self.assertTrue(form.is_valid(), form.errors)
        colaborador = form.save()
        user = colaborador.usuario

        self.assertEqual(user.username, 'joao.silva')
        self.assertEqual(user.auth_source, User.AuthSource.DIRECTORY)
        self.assertFalse(user.has_usable_password())
