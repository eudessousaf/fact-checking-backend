"""Etapa 2 do fluxo: bloco de áudio → falas com falante e tempo.

O provedor é trocável: o resto do sistema só conhece `Transcriber` e
`Segment`, nunca a API de um fornecedor.
"""
