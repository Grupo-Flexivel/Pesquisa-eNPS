from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from .forms import ColaboradorForm, PerfilAcessoForm
from .models import Empresa, PerfilAcesso, User
from .permissoes import contexto_acesso, usuario_tem_acesso
from .test_utils import criar_usuario


class PerfilTests(TestCase):
    def test_perfil_deriva_superuser_e_staff(self):
        admin = criar_usuario('a', administrador=True)
        gestor = criar_usuario('g', modulos=['enps', 'desligamentos', 'talentos'])
        vazio = criar_usuario('v')
        especifico = criar_usuario('e', modulos=['enps'])

        self.assertTrue(admin.is_superuser and admin.is_staff)
        self.assertTrue(gestor.is_staff and not gestor.is_superuser)
        self.assertFalse(vazio.is_staff)  # sem perfil vinculado não entra no painel
        self.assertTrue(especifico.is_staff)

    def test_perfil_vinculado_mas_sem_modulos_nao_da_acesso(self):
        # Perfil recém-criado, ainda sem módulos marcados: não deve abrir o painel.
        perfil_vazio = PerfilAcesso.objects.create(nome='Rascunho')
        usuario = criar_usuario('r', perfil_personalizado=perfil_vazio)
        self.assertFalse(usuario.is_staff)
        self.assertFalse(usuario_tem_acesso(usuario, 'enps'))

    def test_createsuperuser_cria_administrador(self):
        user = User.objects.create_superuser('root', password='x')
        self.assertTrue(user.is_admin_total)
        self.assertTrue(user.is_superuser)

    def test_matriz_de_acesso(self):
        admin = criar_usuario('a', administrador=True)
        gestor = criar_usuario('g', modulos=['enps', 'desligamentos', 'talentos'])
        so_enps = criar_usuario('e', modulos=['enps'])
        so_org = criar_usuario('o', modulos=['talentos.organizacao'])
        talentos = criar_usuario('t', modulos=['talentos'])

        self.assertTrue(usuario_tem_acesso(gestor, 'talentos.busca'))
        self.assertTrue(usuario_tem_acesso(so_enps, 'enps'))
        self.assertFalse(usuario_tem_acesso(so_enps, 'desligamentos'))
        self.assertFalse(usuario_tem_acesso(so_enps, 'talentos.organizacao'))
        self.assertTrue(usuario_tem_acesso(so_org, 'talentos.organizacao'))
        self.assertFalse(usuario_tem_acesso(so_org, 'talentos.busca'))
        self.assertTrue(usuario_tem_acesso(talentos, 'talentos.kanban'))  # chave-pai cobre filhas
        self.assertTrue(contexto_acesso(admin)['administrativo'])
        self.assertFalse(contexto_acesso(gestor)['administrativo'])

    def test_usuario_inativo_perde_acesso(self):
        gestor = criar_usuario('g', modulos=['enps'], is_active=False)
        self.assertFalse(usuario_tem_acesso(gestor, 'enps'))


class AcessoViewsTests(TestCase):
    def setUp(self):
        self.gestor = criar_usuario('g', modulos=['enps', 'desligamentos', 'talentos'])
        self.so_enps = criar_usuario('e', modulos=['enps'])
        self.so_org = criar_usuario('o', modulos=['talentos.organizacao'])

    def test_403_em_modulo_sem_permissao(self):
        self.client.force_login(self.so_enps)
        self.assertEqual(self.client.get(reverse('desligamentos:kanban')).status_code, 403)
        self.assertEqual(self.client.get(reverse('organizacao:departamento_lista')).status_code, 403)
        self.assertEqual(self.client.get(reverse('pesquisas:pesquisa_lista')).status_code, 200)

    def test_anonimo_vai_para_login(self):
        resposta = self.client.get(reverse('organizacao:departamento_lista'))
        self.assertEqual(resposta.status_code, 302)
        self.assertIn('/accounts/login/', resposta.url)

    def test_dashboard_redireciona_usuario_so_de_talentos(self):
        self.client.force_login(self.so_org)
        resposta = self.client.get(reverse('pesquisas:dashboard'))
        self.assertRedirects(resposta, reverse('organizacao:departamento_lista'))

    def test_sidebar_renderiza_conforme_permissao(self):
        self.client.force_login(self.so_org)
        html = self.client.get(reverse('organizacao:departamento_lista')).content.decode()
        self.assertIn('Banco de Talentos', html)
        self.assertNotIn('Gerenciar pesquisas', html)
        self.assertNotIn('Kanban de desligamentos', html)
        self.assertNotIn('Admin Django', html)
        self.assertNotIn('Perfis de acesso', html)  # gestão de perfis é só do Administrador

        self.client.force_login(self.gestor)
        html = self.client.get(reverse('organizacao:departamento_lista')).content.decode()
        self.assertIn('Gerenciar pesquisas', html)
        self.assertIn('Kanban de desligamentos', html)
        self.assertIn('Departamentos', html)
        self.assertNotIn('Admin Django', html)
        self.assertNotIn('Perfis de acesso', html)

    def test_admin_django_e_perfis_de_acesso_so_para_administrador(self):
        self.client.force_login(self.gestor)
        self.assertEqual(self.client.get('/admin/').status_code, 302)
        admin = criar_usuario('adm', administrador=True)
        self.client.force_login(admin)
        self.assertEqual(self.client.get('/admin/').status_code, 200)
        resposta = self.client.get(reverse('organizacao:departamento_lista'))
        self.assertContains(resposta, 'Admin Django')
        self.assertContains(resposta, 'Perfis de acesso')


class ColaboradorFormAcessoTests(TestCase):
    def setUp(self):
        self.empresa = Empresa.objects.create(nome='Matriz')
        self.admin = criar_usuario('a', administrador=True)
        self.perfil = PerfilAcesso.objects.create(nome='Recrutamento', modulos_permitidos=['talentos.organizacao'])
        self.dados = {
            'nome': 'Ana Souza', 'documento': '52998224725', 'empresa': self.empresa.pk, 'ativo': True,
            'perfil': str(self.perfil.pk), 'username': 'ana.souza',
        }

    def test_administrador_concede_perfil_personalizado(self):
        form = ColaboradorForm(self.dados, usuario_logado=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        usuario = form.save().usuario
        self.assertEqual(usuario.perfil_personalizado, self.perfil)
        self.assertFalse(usuario.is_admin_total)
        self.assertTrue(usuario.is_staff)

    def test_administrador_concede_administrador_total(self):
        form = ColaboradorForm({**self.dados, 'perfil': 'ADMINISTRADOR'}, usuario_logado=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        usuario = form.save().usuario
        self.assertTrue(usuario.is_admin_total)
        self.assertIsNone(usuario.perfil_personalizado)

    def test_perfil_sem_username_e_invalido(self):
        form = ColaboradorForm({**self.dados, 'username': ''}, usuario_logado=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)

    def test_dropdown_lista_apenas_perfis_ativos(self):
        inativo = PerfilAcesso.objects.create(nome='Antigo', modulos_permitidos=['enps'], ativo=False)
        form = ColaboradorForm(usuario_logado=self.admin)
        valores = dict(form.fields['perfil'].choices)
        self.assertIn(f'Perfil: {self.perfil.nome}', valores.values())
        self.assertNotIn(str(inativo.pk), valores)

    def test_perfil_inativado_continua_visivel_para_quem_ja_esta_vinculado(self):
        colaborador = ColaboradorForm(self.dados, usuario_logado=self.admin).save()
        self.perfil.ativo = False
        self.perfil.save()
        form = ColaboradorForm(instance=colaborador, usuario_logado=self.admin)
        self.assertEqual(form.fields['perfil'].initial, str(self.perfil.pk))
        valores = dict(form.fields['perfil'].choices)
        self.assertIn(str(self.perfil.pk), valores)
        self.assertIn('(inativo)', valores[str(self.perfil.pk)])

    def test_nao_administrador_nao_ve_nem_altera_campos_de_acesso(self):
        colaborador = ColaboradorForm(self.dados, usuario_logado=self.admin).save()

        gestor = criar_usuario('g', modulos=['enps', 'desligamentos', 'talentos'])
        form = ColaboradorForm(
            {'nome': 'Ana S.', 'documento': '52998224725', 'empresa': self.empresa.pk, 'ativo': True,
             'perfil': 'ADMINISTRADOR', 'username': 'tentativa'},
            instance=colaborador, usuario_logado=gestor,
        )
        self.assertNotIn('perfil', form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        colaborador = form.save()
        colaborador.usuario.refresh_from_db()
        self.assertEqual(colaborador.usuario.perfil_personalizado, self.perfil)  # vínculo intacto
        self.assertFalse(colaborador.usuario.is_admin_total)

    def test_remover_perfil_desvincula_e_tira_acesso(self):
        colaborador = ColaboradorForm(self.dados, usuario_logado=self.admin).save()
        usuario = colaborador.usuario
        form = ColaboradorForm({**self.dados, 'perfil': '', 'username': ''}, instance=colaborador, usuario_logado=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        usuario.refresh_from_db()
        self.assertFalse(usuario.is_staff)
        self.assertIsNone(usuario.perfil_personalizado)


class PerfilAcessoFormTests(TestCase):
    def test_exige_ao_menos_um_modulo(self):
        form = PerfilAcessoForm({'nome': 'Vazio', 'modulos_permitidos': [], 'ativo': True})
        self.assertFalse(form.is_valid())
        self.assertIn('modulos_permitidos', form.errors)

    def test_nome_duplicado_ignora_maiusculas(self):
        PerfilAcesso.objects.create(nome='Gestor de RH', modulos_permitidos=['enps'])
        form = PerfilAcessoForm({'nome': 'gestor de rh', 'modulos_permitidos': ['enps'], 'ativo': True})
        self.assertFalse(form.is_valid())


class PerfilAcessoViewsTests(TestCase):
    def setUp(self):
        self.admin = criar_usuario('adm', administrador=True)
        self.client.force_login(self.admin)

    def test_apenas_administrador_acessa(self):
        gestor = criar_usuario('g', modulos=['enps'])
        self.client.force_login(gestor)
        self.assertEqual(self.client.get(reverse('pesquisas:perfil_lista')).status_code, 403)
        self.assertEqual(self.client.get(reverse('pesquisas:perfil_criar')).status_code, 403)

        self.client.logout()
        self.assertEqual(self.client.get(reverse('pesquisas:perfil_lista')).status_code, 302)

    def test_criar_listar_e_editar_perfil(self):
        resposta = self.client.post(reverse('pesquisas:perfil_criar'), {
            'nome': 'Gestor de Talentos', 'modulos_permitidos': ['talentos.organizacao', 'talentos.curriculos'], 'ativo': True,
        })
        self.assertRedirects(resposta, reverse('pesquisas:perfil_lista'))
        perfil = PerfilAcesso.objects.get(nome='Gestor de Talentos')
        self.assertEqual(set(perfil.modulos_permitidos), {'talentos.organizacao', 'talentos.curriculos'})

        self.assertContains(self.client.get(reverse('pesquisas:perfil_lista')), 'Gestor de Talentos')

        resposta = self.client.post(reverse('pesquisas:perfil_editar', args=[perfil.pk]), {
            'nome': 'Gestor de Talentos', 'modulos_permitidos': ['talentos'], 'ativo': True,
        })
        self.assertRedirects(resposta, reverse('pesquisas:perfil_lista'))
        perfil.refresh_from_db()
        self.assertEqual(perfil.modulos_permitidos, ['talentos'])

    def test_alternar_status(self):
        perfil = PerfilAcesso.objects.create(nome='X', modulos_permitidos=['enps'])
        self.client.post(reverse('pesquisas:perfil_status', args=[perfil.pk]))
        perfil.refresh_from_db()
        self.assertFalse(perfil.ativo)

    def test_excluir_perfil_sem_uso(self):
        perfil = PerfilAcesso.objects.create(nome='Descartável', modulos_permitidos=['enps'])
        self.client.post(reverse('pesquisas:perfil_excluir', args=[perfil.pk]))
        self.assertFalse(PerfilAcesso.objects.filter(pk=perfil.pk).exists())

    def test_excluir_perfil_em_uso_e_bloqueado(self):
        perfil = PerfilAcesso.objects.create(nome='Em uso', modulos_permitidos=['enps'])
        criar_usuario('vinculado', perfil_personalizado=perfil)
        resposta = self.client.post(reverse('pesquisas:perfil_excluir', args=[perfil.pk]), follow=True)
        self.assertContains(resposta, 'não pode ser excluído')
        self.assertTrue(PerfilAcesso.objects.filter(pk=perfil.pk).exists())

    def test_status_e_exclusao_nao_aceitam_get(self):
        perfil = PerfilAcesso.objects.create(nome='Y', modulos_permitidos=['enps'])
        self.assertEqual(self.client.get(reverse('pesquisas:perfil_excluir', args=[perfil.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse('pesquisas:perfil_status', args=[perfil.pk])).status_code, 405)


class ModeloProtegidoTests(TestCase):
    def test_perfil_em_uso_nao_pode_ser_excluido_diretamente(self):
        perfil = PerfilAcesso.objects.create(nome='Protegido', modulos_permitidos=['enps'])
        criar_usuario('vinculado', perfil_personalizado=perfil)
        with self.assertRaises(ProtectedError):
            perfil.delete()


class TelasAdministrativasTests(TestCase):
    def setUp(self):
        self.admin = criar_usuario('adm', administrador=True)
        self.client.force_login(self.admin)

    def test_admin_de_usuarios_renderiza_e_salva_perfil(self):
        perfil = PerfilAcesso.objects.create(nome='Atendimento', modulos_permitidos=['enps'])
        alvo = criar_usuario('alvo')
        self.assertContains(self.client.get('/admin/pesquisas/user/add/'), 'Acesso aos módulos')
        url = f'/admin/pesquisas/user/{alvo.pk}/change/'
        self.assertContains(self.client.get(url), 'Acesso aos módulos')
        resposta = self.client.post(url, {
            'username': 'alvo', 'first_name': '', 'last_name': '', 'email': '', 'is_active': 'on',
            'is_admin_total': '', 'perfil_personalizado': perfil.pk,
            'auth_source': 'LOCAL', 'local_password': '',
            'last_login_0': '', 'last_login_1': '', 'date_joined_0': '2026-01-01', 'date_joined_1': '00:00:00',
            'password': alvo.password,
        })
        self.assertEqual(resposta.status_code, 302, getattr(resposta, 'context', None) and resposta.context['adminform'].form.errors)
        alvo.refresh_from_db()
        self.assertEqual(alvo.perfil_personalizado, perfil)
        self.assertTrue(alvo.is_staff)

    def test_formulario_de_colaborador_mostra_campos_de_acesso_so_ao_administrador(self):
        self.assertContains(self.client.get(reverse('pesquisas:colaborador_criar')), 'name="perfil"')
        gestor = criar_usuario('g', modulos=['enps'])
        self.client.force_login(gestor)
        self.assertNotContains(self.client.get(reverse('pesquisas:colaborador_criar')), 'name="perfil"')
