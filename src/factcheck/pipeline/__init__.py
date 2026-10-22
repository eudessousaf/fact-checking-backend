"""Estágios do fluxo. Cada um pega no banco o que está pendente, faz, e grava
o novo estado na mesma transação — por isso cair e voltar não duplica nada."""
