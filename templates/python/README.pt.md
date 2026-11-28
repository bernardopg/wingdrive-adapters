# Notas de texto

Modelo de adapter do WingDrive escrito em Python. Ele cria uma nota para cada arquivo `.txt` no
topo de uma pasta. Copie esta pasta para começar o seu adapter.

## Configuração

1. Instale o adapter, abra **Fontes > Adicionar fonte** no WingDrive e escolha **Text notes**.
2. Escolha a pasta com os seus arquivos `.txt`.

A primeira sincronização lê todos os arquivos. As seguintes leem só os que mudaram.

## Dados

Cada nota guarda o nome do arquivo sem `.txt` (`title`), o texto até 200.000 caracteres (`body`),
a contagem de palavras (`words`) e a data de modificação do arquivo (`modified`). Arquivos em
subpastas e com outras extensões ficam de fora. Quando você apaga um arquivo, a nota dele some na
próxima sincronização.

## Privacidade

O adapter lê só a pasta que você escolher. Ele não usa rede nem conta. Nada sai do seu computador.
