from pgvector.django import VectorField


class EmbeddingField(VectorField):
    """VectorField que também funciona fora do PostgreSQL.

    Em produção é o tipo `vector(N)` do pgvector. Em SQLite (usado só nos testes
    automatizados) é armazenado como texto; consultas por distância exigem PostgreSQL.
    """

    def db_type(self, connection):
        if connection.vendor != 'postgresql':
            return 'text'
        return super().db_type(connection)
