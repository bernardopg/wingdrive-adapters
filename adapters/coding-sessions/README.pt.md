# Sessões de agentes de código

Busque nas sessões que você teve com agentes de código junto com os seus arquivos: o que você
pediu, o que o agente respondeu, as ferramentas que ele rodou e os arquivos que editou. Lê as
transcrições que o **Claude Code** e o **Codex** guardam neste computador.

## Configuração

1. Instale o adapter em **Fontes > Adicionar fonte > Adapters**.
2. Adicione uma fonte. As pastas vêm preenchidas quando o Claude Code (`~/.claude/projects`) ou o
   Codex (`~/.codex/sessions`) é encontrado; deixe uma pasta vazia para usar o padrão ou escolha
   outra.

Um agente que não está instalado é ignorado. A primeira sincronização lê todas as transcrições,
cerca de meio minuto para mil sessões; as seguintes leem só as sessões que mudaram.

## Dados

Um registro por sessão: título (o que você deu no Claude Code, ou o primeiro pedido), texto (seus
pedidos e as respostas do agente, até 200.000 caracteres), agente, pasta do projeto, branch, datas
da primeira e da última mensagem, número de pedidos, ferramentas usadas e arquivos editados.

Ficam de fora a saída das ferramentas, o raciocínio do modelo, as transcrições de subagentes e o
texto que os agentes acrescentam aos seus turnos. Uma transcrição apagada remove a sessão na
próxima sincronização.

## Privacidade

Transcrições costumam ter código, caminhos e credenciais. Antes de guardar qualquer coisa, o
adapter troca o que parece segredo por `[secret]`: chaves de API (Anthropic, OpenAI, GitHub,
GitLab, Slack, AWS, Google), tokens JWT, chaves privadas e valores escritos como `password=`,
`token:` ou `api_key =`. É um melhor esforço: uma senha escrita em palavras comuns fica como está.

O adapter lê só as duas pastas, não usa rede nem conta. Nada sai do seu computador.
