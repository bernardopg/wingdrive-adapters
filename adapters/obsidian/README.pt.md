# Obsidian

Traz as notas de um cofre do Obsidian para uma biblioteca do WingDrive, e elas aparecem na busca junto
com os seus arquivos, com tags, aliases e os links entre elas. Funciona com qualquer pasta de
arquivos Markdown, não só com cofres do Obsidian.

## Configuração

1. No WingDrive, abra **Fontes > Adicionar fonte > Adaptadores** e instale o **Obsidian**, ou rode
   `wing adapters install obsidian`.
2. Adicione uma fonte. O cofre que você abriu por último no Obsidian já vem preenchido, e os outros
   aparecem como opção; qualquer pasta de arquivos Markdown também serve.
3. Se quiser, liste pastas ou arquivos para ignorar, separados por vírgula:
   `Templates, Arquivo/*, *.excalidraw.md`.

O adaptador precisa do Python 3.9 ou mais novo (`python3` no Linux e no macOS, `python` no Windows).
Ele usa só a biblioteca padrão.

## Dados

Um registro por nota Markdown:

| Campo | Origem |
|---|---|
| `title` | `title` no frontmatter, ou o nome do arquivo |
| `body` | A nota sem o frontmatter e sem um primeiro título que só repete o nome da nota (primeiros 200.000 caracteres) |
| `path`, `folder` | O caminho dentro do cofre |
| `tags` | `tags` do frontmatter e `#tags` no texto, fora de blocos de código |
| `aliases` | `aliases` do frontmatter |
| `created` | `created` ou `date` do frontmatter, senão a data de criação ou de modificação do arquivo |
| `modified` | A data de modificação do arquivo |
| `word_count` | Palavras fora de blocos de código e comentários |

As notas se ligam às notas que citam por `[[wikilinks]]` (inclusive `[[Nota#Título]]`,
`[[Nota|texto]]` e embeds `![[Nota]]`) e por links `[texto](Nota.md)`. Os links se resolvem como no
Obsidian: pelo caminho, depois pelo nome do arquivo (vence o caminho mais curto), depois pelo alias.

Ficam de fora: pastas e arquivos ocultos (`.obsidian`, `.trash`, `.git`), `node_modules`, links
simbólicos e os padrões que você listar.

A primeira sincronização lê todas as notas. As seguintes leem só as notas cujo tamanho ou data de
modificação mudou, apagam os registros das notas que sumiram e atualizam os links, inclusive links
cuja nota de destino foi criada depois deles.

## Privacidade

O adaptador lê só a pasta do cofre que você escolher, mais a lista de cofres do próprio Obsidian
(`obsidian.json`) para sugerir um. Ele não usa a rede, não roda outros programas e não grava em
lugar nenhum. Suas notas ficam na sua biblioteca do WingDrive, neste computador. No Linux ele roda
isolado e não enxerga nada além disso.
