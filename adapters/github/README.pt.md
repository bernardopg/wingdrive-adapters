# GitHub

Traz uma conta do GitHub para uma biblioteca do WingDrive, e repositórios, estrelas, issues e pull
requests aparecem na busca junto com os seus arquivos.

## Configuração

1. No WingDrive, abra **Fontes > Adicionar fonte > Adaptadores** e instale o **GitHub**, ou rode
   `wing adapters install github`.
2. Adicione uma fonte. O usuário já vem preenchido quando o GitHub CLI (`gh`) está conectado neste
   computador; senão, digite o seu.
3. Se quiser, cole um token de acesso para incluir repositórios privados, issues e pull requests:
   - um **token fine-grained** (Settings > Developer settings > Fine-grained tokens) com leitura de
     *Metadata* e *Issues* (e *Pull requests*) nos repositórios que você quiser, ou
   - um **token clássico** com o escopo `repo`, ou a saída de `gh auth token`.

O adaptador precisa do Python 3.9 ou mais novo e usa só a biblioteca padrão.

## Dados

Um registro por item, e o campo `kind` diz de que tipo ele é:

| Tipo | O quê | Precisa de token |
|---|---|---|
| `repository` | Repositórios seus (`relation` = `owner`) e com estrela (`starred`), com descrição, tópicos, linguagem e estrelas | Públicos: não. Privados: sim |
| `issue` | Issues que você abriu, em que foi atribuído ou mencionado, ou que acompanha | Sim |
| `pull_request` | O mesmo para pull requests; os que entraram têm `state` = `merged` | Sim |

Cada registro tem `title`, `body`, `repo`, `number`, `state`, `author`, `labels`, `url`, `created` e
`updated`. Issues e pull requests também têm `comments`: o texto dos comentários, do mais antigo ao
mais novo, para a busca achar uma issue pelo que foi dito nela.

Com um token, cada comentário também vira um registro `comment` que pertence à sua issue ou pull
request, com `body`, `author`, `url`, `created` e `updated`. Desligue isso em **Comentários** para
economizar requisições. Comentários de revisão nas linhas do diff de um pull request ficam de fora.

Toda sincronização lista os seus repositórios de novo (uma requisição a cada 100) e remove os que
você apagou ou tirou a estrela. Issues e pull requests chegam de forma incremental: só os que mudaram
desde a última sincronização. Uma issue que mudou tem os comentários listados de novo (uma requisição a
cada 100 comentários), e é assim que comentários editados e apagados são notados. Sem token, o
GitHub permite 60 requisições por hora, com token 5.000; quando o limite chega, a sincronização para
e continua na próxima. A primeira sincronização de uma conta com milhares de issues lista
comentários por no máximo 10 minutos e continua nas seguintes. Uma fonte criada antes da 1.1.0
guarda os comentários, mas o índice de busca dela só passa a lê-los depois que a fonte é adicionada
de novo.

## Privacidade

O adaptador fala só com `api.github.com`, usando o token que você der, e só lê: nunca cria, altera
ou apaga nada no GitHub. Para sugerir o seu usuário ele lê a linha `user:` do `hosts.yml` do GitHub
CLI, nunca o token dele. O que ele busca fica na sua biblioteca do WingDrive, neste computador. No
Linux ele roda isolado e não enxerga nada além do que está listado.
