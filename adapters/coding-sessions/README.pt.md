# Sessões de agentes de código

Busque nas sessões que você teve com agentes de código junto com os seus arquivos: o que você
pediu, o que o agente respondeu, as ferramentas que ele rodou e os arquivos que editou. Lê as
sessões que o **Claude Code**, o **Codex** e o **OpenCode** guardam neste computador.

## Configuração

1. Instale o adapter em **Fontes > Adicionar fonte > Adapters**.
2. Adicione uma fonte. Os campos vêm preenchidos quando o Claude Code (`~/.claude/projects`), o
   Codex (`~/.codex/sessions`) ou o OpenCode (`~/.local/share/opencode/opencode.db`, também no
   macOS e no Windows) é encontrado; deixe um campo vazio para usar o padrão ou escolha outro local.

Um agente que não está instalado é ignorado. A primeira sincronização lê todas as sessões, cerca de
meio minuto para mil sessões. As seguintes leem só o que mudou: as linhas acrescentadas a uma
transcrição do Claude Code ou do Codex desde a última sincronização e as sessões do OpenCode com
mensagens novas.

## Dados

Um registro por sessão: título (o que a sessão tem no agente, ou o primeiro pedido), resumo (o
primeiro pedido e a última resposta, até 400 caracteres cada, mostrado como prévia), texto (todos
os seus pedidos e as respostas do agente, completos; é o que a busca lê), agente, pasta do projeto,
branch (o OpenCode não registra), datas da primeira e da última mensagem, número de pedidos,
ferramentas usadas e arquivos editados (caminhos relativos ganham a pasta da sessão).

Cada ferramenta (registros `tool`, pelo nome) e cada arquivo editado (registros `file`, pelo
caminho) também é um registro, ligado às sessões que o usaram. Uma ferramenta ou um arquivo que
nenhuma sessão usa mais é removido.

Ficam de fora a saída das ferramentas, o raciocínio do modelo, as sessões de subagentes e o texto
que os agentes acrescentam aos seus turnos. Uma transcrição ou sessão do OpenCode apagada remove o
registro na próxima sincronização.

Para continuar uma transcrição de onde a última sincronização parou, o adapter guarda o que leu de
cada uma, já sem segredos e comprimido, na pasta de estado da própria fonte, dentro da pasta de
dados do WingDrive. Uma transcrição que encolheu ou mudou antes desse ponto é lida de novo desde o
início.

## Privacidade

Transcrições costumam ter código, caminhos e credenciais. Antes de guardar qualquer coisa, o
adapter troca o que parece segredo por `[secret]`: chaves de API (Anthropic, OpenAI, GitHub,
GitLab, Slack, AWS, Google), tokens JWT, chaves privadas e valores escritos como `password=`,
`token:` ou `api_key =`. É um melhor esforço: uma senha escrita em palavras comuns fica como está.

O adapter lê só as duas pastas e o arquivo de banco do OpenCode (aberto só para leitura; o
`auth.json` e o resto da pasta do OpenCode ficam fora de alcance), não usa rede nem conta. Nada sai
do seu computador.
