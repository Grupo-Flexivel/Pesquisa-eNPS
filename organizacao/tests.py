from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from pesquisas.models import User
from pesquisas.test_utils import criar_usuario

from .models import Cargo, Departamento, Secao


class ArvoreTests(TestCase):
    def setUp(self):
        self.dep = Departamento.objects.create(nome='Tecnologia')
        self.secao = Secao.objects.create(departamento=self.dep, nome='Dev')
        self.cargo = Cargo.objects.create(secao=self.secao, nome='Front-end')

    def test_nao_exclui_com_filhos(self):
        with self.assertRaises(ProtectedError):
            self.dep.delete()
        with self.assertRaises(ProtectedError):
            self.secao.delete()
        self.assertTrue(Departamento.objects.filter(pk=self.dep.pk).exists())

    def test_exclui_sem_dependencias(self):
        self.cargo.delete()
        self.secao.delete()
        self.dep.delete()
        self.assertFalse(Departamento.objects.exists())

    def test_exclusao_em_massa_tambem_e_protegida(self):
        with self.assertRaises(ProtectedError):
            Departamento.objects.filter(pk=self.dep.pk).delete()
        self.assertTrue(Departamento.objects.filter(pk=self.dep.pk).exists())

    def test_nome_unico_sem_diferenciar_maiusculas(self):
        with self.assertRaises(ValidationError):
            Departamento(nome='tecnologia').full_clean()
        # mesmo nome em outro departamento é permitido
        outro = Departamento.objects.create(nome='RH')
        Secao(departamento=outro, nome='dev').full_clean()


class TelasTests(TestCase):
    def setUp(self):
        self.user = criar_usuario('o', modulos=['talentos.organizacao'])
        self.client.force_login(self.user)
        self.dep = Departamento.objects.create(nome='Tecnologia')

    def test_crud_basico(self):
        resposta = self.client.post(
            reverse('organizacao:secao_criar'), {'departamento': self.dep.pk, 'nome': 'Dev', 'ativo': 'on'},
        )
        self.assertRedirects(resposta, reverse('organizacao:secao_lista'))
        secao = Secao.objects.get()
        self.client.post(reverse('organizacao:cargo_criar'), {'secao': secao.pk, 'nome': 'Front-end', 'ativo': 'on'})
        self.assertEqual(Cargo.objects.count(), 1)
        self.assertContains(self.client.get(reverse('organizacao:cargo_lista')), 'Front-end')

    def test_excluir_com_dependencia_mostra_erro_e_preserva(self):
        Secao.objects.create(departamento=self.dep, nome='Dev')
        resposta = self.client.post(reverse('organizacao:departamento_excluir', args=[self.dep.pk]), follow=True)
        self.assertContains(resposta, 'não pode ser excluído')
        self.assertTrue(Departamento.objects.filter(pk=self.dep.pk).exists())

    def test_alternar_status_e_pai_inativo_nao_aparece_no_form(self):
        self.client.post(reverse('organizacao:departamento_status', args=[self.dep.pk]))
        self.dep.refresh_from_db()
        self.assertFalse(self.dep.ativo)
        resposta = self.client.get(reverse('organizacao:secao_criar'))
        self.assertNotContains(resposta, '>Tecnologia<')

    def test_status_e_exclusao_nao_aceitam_get(self):
        self.assertEqual(self.client.get(reverse('organizacao:departamento_excluir', args=[self.dep.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse('organizacao:departamento_status', args=[self.dep.pk])).status_code, 405)

    def test_usuario_sem_modulo_recebe_403(self):
        outro = criar_usuario('x', modulos=['enps'])
        self.client.force_login(outro)
        self.assertEqual(self.client.get(reverse('organizacao:cargo_lista')).status_code, 403)
        self.assertEqual(
            self.client.post(reverse('organizacao:departamento_excluir', args=[self.dep.pk])).status_code, 403,
        )
        self.assertTrue(Departamento.objects.filter(pk=self.dep.pk).exists())
