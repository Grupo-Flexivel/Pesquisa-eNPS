"""Sanitização do HTML produzido pelo editor de texto (TinyMCE) antes de salvar.

A descrição do formulário é exibida com `|safe` para o gestor; por isso só passam as tags e
estilos de formatação que o editor gera — scripts, eventos (onclick…), iframes e URLs
`javascript:` são removidos.
"""
import nh3


TAGS_PERMITIDAS = {
    'p', 'br', 'hr', 'div', 'span', 'strong', 'b', 'em', 'i', 'u', 's', 'sub', 'sup',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'code',
    'ul', 'ol', 'li', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'caption',
}
ATRIBUTOS_PERMITIDOS = {
    '*': {'style'},
    'a': {'href', 'title', 'target'},
    'td': {'colspan', 'rowspan'},
    'th': {'colspan', 'rowspan'},
}
ESTILOS_PERMITIDOS = {
    'color', 'background-color', 'text-align', 'text-decoration', 'font-weight', 'font-style', 'padding-left',
}


def limpar_html(html):
    if not html:
        return ''
    return nh3.clean(
        html,
        tags=TAGS_PERMITIDAS,
        attributes=ATRIBUTOS_PERMITIDOS,
        filter_style_properties=ESTILOS_PERMITIDOS,
        url_schemes={'http', 'https', 'mailto'},
        link_rel='noopener noreferrer',
    )
