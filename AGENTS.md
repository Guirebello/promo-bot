# AGENTS.md

Guia pra agentes. O README é pra humanos, este arquivo tem o setup, a arquitetura e as regras.

## Resumo

- Código todo em `promo_bot.py`. Python 3.10+, única dependência `telethon` 1.x, listada em `requirements.txt`.
- Um processo com duas sessões Telethon, as duas em MTProto. `sessao_usuario` é a conta do dono, `sessao_bot` é o bot do BotFather.
- Roda como serviço systemd de usuário, `promo-bot.service`, que espera o repo em `~/promo-bot`.
- Estado em `state.json`, credenciais em `.env`. Os dois ficam na pasta do repo e o git ignora.
- Mensagens do bot e comentários do código são em PT-BR.

## Setup numa máquina nova

Pré-requisitos: Linux com systemd, Python 3.10+, internet de saída, máquina que não suspende.

1. Deixe o repo em `~/promo-bot`. Se ficar em outro lugar, ajuste `WorkingDirectory` e `ExecStart` no `.service`.
2. Crie o venv:
   ```bash
   cd ~/promo-bot && python3 -m venv venv && venv/bin/pip install -r requirements.txt
   ```
   Debian/Ubuntu sem o pacote `python3-venv` e sem sudo: `python3 -m venv --without-pip venv`, baixe `https://bootstrap.pypa.io/pip/pip.pyz` e rode `venv/bin/python pip.pyz install -r requirements.txt`.
3. Instale o serviço sem habilitar. Quem habilita e inicia é o `setup`.
   ```bash
   mkdir -p ~/.config/systemd/user && cp promo-bot.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   loginctl enable-linger
   ```
   Se `enable-linger` falhar por permissão, peça pro usuário rodar com sudo. Sem linger o serviço só sobe quando o usuário faz login.
4. Confira se a máquina suspende: `HandleLidSwitch*` em `/etc/systemd/logind.conf` e `systemd-inhibit --list`. Se suspender com a tampa fechada, pergunte antes de mudar qualquer coisa.
5. Peça ao usuário que tenha em mãos, sem colar no chat:
   - `api_id` e `api_hash` de my.telegram.org, em "API development tools". Reusar um app antigo funciona.
   - token do bot, via @BotFather `/newbot`. Se for um bot antigo que usava webhook, abrir `https://api.telegram.org/bot<TOKEN>/deleteWebhook` antes.
6. O usuário roda no terminal dele, porque o comando é interativo e precisa de TTY:
   ```bash
   cd ~/promo-bot && venv/bin/python promo_bot.py setup
   ```
   Numa máquina remota: `ssh -t <host> 'cd promo-bot && venv/bin/python promo_bot.py setup'`.
   O comando pede api_id, api_hash, token, telefone no formato `+55...`, o código que chega no app do Telegram e a senha 2FA se houver. Ele grava `.env` com permissão 600, cria as sessões e roda `systemctl --user enable` e `restart`. Termina com `✅ Rodando`.
7. Verifique:
   ```bash
   systemctl --user is-active promo-bot
   journalctl --user -u promo-bot -n 20     # esperado: "INFO promo: Rodando: N grupos, M canais"
   ```
   Depois peça pro usuário mandar `/help` ao bot.

## Operação

| Tarefa | Comando |
|---|---|
| status | `systemctl --user status promo-bot` |
| logs | `journalctl --user -u promo-bot -f` |
| subir código novo | copiar `promo_bot.py` pra `~/promo-bot/` e `systemctl --user restart promo-bot` |
| refazer login | `venv/bin/python promo_bot.py setup`, que para o serviço sozinho |
| antes de mudar o formato do estado | `cp state.json state.json.bak-<motivo>` |

## Arquitetura

Caminho de um post:

1. `poll_loop` chama `poll()` e dorme `random.uniform(5, 15)` segundos.
2. `poll()` faz um único `messages.GetPeerDialogs` com todos os canais vigiados e pega o `top_message` de cada um. Compara com `channels[*].last_id`. Canal sem `last_id` só é inicializado, o histórico não é processado.
3. Se `top > last_id`: `get_messages(min_id=last_id, limit=100)`, ordena do mais antigo pro mais novo, agrupa álbuns com `albums()`, chama `relay()` por lote, avança `last_id` a cada lote e salva.
4. `relay()` testa `matching(state.keywords_for(chat_id), texto)`. Se bater, a conta encaminha o post pro canal de apoio, `state.relay`.
5. O bot é admin do canal de apoio. `on_relay` e `on_relay_album` recebem o post, encaminham pro dono em `state.owner` e apagam do canal de apoio.
6. Canal com conteúdo protegido levanta `ChatForwardsRestrictedError`. Nesse caso o bot manda texto e link.

Na primeira execução, `prepare()` cria o canal de apoio, privado, silenciado e arquivado, põe o bot como admin e manda `/start` da conta pro bot. Se o canal de apoio sumir, ele cria outro.

Por que é assim. Não simplifique sem ler isto:

- Bot não lê canal onde não é admin. Por isso a leitura é feita pela conta.
- Mensagem enviada pela própria conta não notifica o dono. Por isso a entrega é feita pelo bot.
- O bot não acessa mensagens da conta. Os dois se encontram no canal de apoio. Encaminhar um encaminhamento preserva o "Encaminhado de <canal original>".
- O Telegram não empurra updates de canal grande pra sessões que não estão com o chat aberto. Por isso é polling e não `events.NewMessage` na conta. Com eventos, um post chegou 12 minutos atrasado.
- O intervalo sorteado de 5 a 15 s e a desaceleração 2x por 30 min depois de um `FloodWaitError` existem pra reduzir o padrão de robô. Não baixe de 5 s.

Login caído: `UnauthorizedError` ou `AuthKeyDuplicatedError` fazem `poll_loop` retornar e o processo sai. O systemd reinicia, `run()` vê a conta deslogada, sobe só o bot, manda `LOGGED_OUT` pro dono e fica parado até o próximo `setup`. Assim não entra em crash loop.

Comandos: `on_command` despacha pros métodos `cmd_*` e só responde a `state.owner`.

Argumento de `/add` e `/rm`: `split_groups()` tira o `#grupo`, `split_top()` separa as palavras por vírgula fora de colchetes e `parse_entries()` usa `EXCL_RE` pra extrair exclusões, tanto `-[a, b c]` quanto o atalho `-palavra`. Colchete sem par ou exclusão sem palavra antes levantam `UserError`. `/add` numa palavra que já existe só soma as exclusões novas. `/rm` com `-[...]` tira só as exclusões, sem `-[...]` tira a palavra inteira.

## Estado

```json
{
  "groups": {
    "geral": {"three houses": [], "switch 2": ["case", "capa protetora"]},
    "pc": {"ssd": [], "rtx 4060": []}
  },
  "channels": {
    "hardware": {"id": -1001234567890, "title": "...", "link": "https://t.me/...", "groups": ["pc"], "last_id": 123}
  },
  "relay": -1009876543210,
  "started": true,
  "owner": 123456789
}
```

- `geral` sempre existe e todo canal lê. Não pode ser apagado nem desligado de um canal.
- Apelidos ficam em minúsculo. Nomes de grupo passam por `norm()`.
- `groups` mapeia grupo pra `{palavra: [exclusões]}`. A ordem de inserção é preservada e o `/list` usa ela.
- Formatos antigos migram em `State.__init__`. `{"keywords": [...]}` vira `groups.geral`, e grupo salvo como lista vira dict sem exclusões. O formato novo é gravado no próximo `save()`. Qualquer mudança de formato precisa de migração ali.
- `save()` é atômico, grava num arquivo temporário e usa `os.replace`.

## Matching

- `norm()` deixa tudo minúsculo, tira acento e junta espaços.
- `keyword_re()` exige início de palavra com `(?<!\w)`, liga os termos com `[\W_]*` e não ancora o fim.
- Resultado: `fone` pega "fones", `tv` não pega "atv", `air fryer` pega "airfryer", `three houses` não pega "three" sozinho.
- `exclusion_re()` é mais estrito. Exige palavra inteira com `(?!\w)` no fim e aceita plural `(?:e?s)?` em cada termo. `capa` pega "capas" e não pega "capacidade". A escolha é de propósito: exclusão que bate demais faz o usuário perder promoção.
- `matching(entries, texto)` recebe `[(palavra, exclusões)]` vindo de `State.keywords_for()`. Uma palavra só conta se ela aparece e nenhuma das exclusões dela aparece. A exclusão vale só pra palavra dela, então outro termo pode encaminhar o mesmo post.

## Regras

- Nunca leia, imprima, copie pra fora da máquina ou commite `.env`, `*.session*` e `state.json`.
- Nunca abra `sessao_usuario.session` em dois processos ao mesmo tempo. Isso inclui rodar o script com o serviço ativo e usar uma cópia da sessão em outra máquina. O Telegram responde com `AUTH_KEY_DUPLICATED` e derruba a sessão.
- Não teste chamando a API real em loop. A lógica pura, `norm`, `matching`, `parse_entries`, `parse_link`, `albums` e os `cmd_*`, dá pra testar com `App.__new__(App)`, um `State` num arquivo temporário e clientes falsos.
- Commits seguem Conventional Commits sem escopo, por exemplo `feat: add x`.
