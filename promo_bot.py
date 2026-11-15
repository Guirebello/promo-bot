"""promo-bot: encaminha pra você posts de canais do Telegram que tenham suas palavras-chave.

Sua conta checa os canais vigiados a cada 5–15 s (bot não lê canal onde não é admin;
e o Telegram não empurra posts de canal grande em tempo real). Quando um post bate,
ela encaminha pra um canal de apoio privado, e o bot encaminha de lá pra você e apaga.
Assim a mensagem chega pelo bot (com notificação) e mantém o "Encaminhado de <canal>".

Uso:
  venv/bin/python promo_bot.py setup   # 1ª vez: credenciais + login (interativo)
  venv/bin/python promo_bot.py         # roda (o systemd chama este)
"""
import asyncio
import getpass
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from telethon import TelegramClient, errors, events, functions, types, utils

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / ".env"
STATE_FILE = BASE / "state.json"
SERVICE = "promo-bot"
RELAY_TITLE = "promo-bot (não apague)"
# intervalo sorteado entre checagens: evita ritmo cravado de robô
POLL_MIN, POLL_MAX = 5, 15
SLOWDOWN_SECONDS = 30 * 60  # após FLOOD_WAIT, checa 2x mais devagar por este tempo
LOGGED_OUT = (
    "⚠️ O login da sua conta caiu (encerrado pelo Telegram ou em Dispositivos).\n"
    "No notebook, rode: cd ~/promo-bot && venv/bin/python promo_bot.py setup"
)

GERAL = "geral"  # grupo lido por todos os canais; não pode ser apagado
COMMANDS = [
    ("add", "vigiar: /add <palavra> [-[ignorar, ...]] [#grupo]"),
    ("rm", "parar de vigiar ou de ignorar: /rm <palavra> [-[...]] [#grupo]"),
    ("rmgroup", "apagar grupo: /rmgroup #grupo"),
    ("watch", "vigiar canal: /watch <link> <apelido> [#grupos]"),
    ("unwatch", "parar de vigiar canal: /unwatch <apelido>"),
    ("link", "canal passa a ler grupo: /link <apelido> #grupo"),
    ("unlink", "canal para de ler grupo: /unlink <apelido> #grupo"),
    ("list", "grupos, palavras e canais"),
    ("help", "comandos"),
]
HELP = """Palavras (sem #grupo vão pro geral, que todo canal lê):
/add <palavras> [#grupo] — vigiar (várias: separe por vírgula; grupo novo é criado)
/add <palavra> -[a, b] — ignorar posts dessa palavra que citem "a" ou "b" (soma à lista)
/rm <palavra> -[a] — parar de ignorar "a"
/rm <palavras> [#grupo] — parar de vigiar (some junto com o que ela ignora)
/rmgroup #grupo — apagar grupo

Canais:
/watch <link> <apelido> [#grupos] — vigiar canal
/unwatch <apelido> — parar de vigiar canal
/link <apelido> #grupo — canal passa a ler o grupo
/unlink <apelido> #grupo — canal para de ler o grupo

/list — grupos, palavras e canais
/help — esta ajuda

Ex: /add ssd, rtx 4060 #pc  e depois  /link hardware #pc
Ex: /add switch 2 -[case, capa protetora]
Post de canal com alguma palavra que ele lê é encaminhado pra cá.
Maiúscula e acento não importam. Exclusão bate por palavra inteira
(e plural): -[capa] corta "capas", mas não "capacidade"."""

CMD_RE = re.compile(r"^/(?P<cmd>\w+)(?:@\w+)?(?:\s+(?P<arg>.*))?$", re.S)
GROUP_RE = re.compile(r"#([\w-]+)")
# exclusões: "-[a, b c]" (lista) ou "-palavra" (atalho pra uma só)
EXCL_RE = re.compile(r"(?:^|\s)-(?:\[([^\]]*)\]|([^\s\[\],]+))")
LINK_RE = re.compile(
    r"^(?:https?://)?(?:t|telegram)\.me/"
    r"(?:(?:joinchat/|\+)(?P<invite>[\w-]+)|c/(?P<cid>\d+)|(?:s/)?(?P<user>[a-z]\w{3,}))",
    re.I,
)
ALIAS_RE = re.compile(r"[\w-]+")

log = logging.getLogger("promo")


class UserError(Exception):
    """Erro com mensagem pronta pra mostrar no chat."""


# ---------- palavras ----------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def keyword_re(kw: str) -> re.Pattern:
    # começa em início de palavra ("tv" não pega "atv", "fone" pega "fones");
    # separador entre palavras opcional ("air fryer" pega "airfryer" e "air-fryer")
    return re.compile(r"(?<!\w)" + r"[\W_]*".join(map(re.escape, norm(kw).split())))


def exclusion_re(ex: str) -> re.Pattern:
    # palavra inteira com plural opcional: "capa" pega "capas", mas não "capacidade"
    words = [re.escape(w) + r"(?:e?s)?" for w in norm(ex).split()]
    return re.compile(r"(?<!\w)" + r"[\W_]*".join(words) + r"(?!\w)")


def matching(entries: list[tuple[str, list[str]]], text: str) -> list[str]:
    """entries: [(palavra, exclusões)]. Bate se a palavra aparece e nenhuma exclusão dela aparece."""
    t = norm(text)
    return [
        k for k, excl in entries
        if keyword_re(k).search(t) and not any(exclusion_re(x).search(t) for x in excl)
    ]


def split_top(arg: str) -> list[str]:
    """Separa por vírgula, menos as vírgulas dentro de [...]."""
    parts, depth, cur = [], 0, ""
    for ch in arg:
        depth += (ch == "[") - (ch == "]" and depth > 0)
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return parts + [cur]


def parse_entries(arg: str) -> list[tuple[str, list[str]]]:
    """'switch 2 -[case, capa], ps5 -controle' -> [('switch 2', ['case', 'capa']), ('ps5', ['controle'])]"""
    entries = []
    for item in split_top(arg):
        if not norm(item):
            continue
        excl = []
        for lst, one in EXCL_RE.findall(item):
            excl += [x.strip() for x in (lst.split(",") if lst else [one]) if norm(x)]
        kw = EXCL_RE.sub(" ", item).strip()
        if "[" in kw or "]" in kw:
            raise UserError(f"Colchete sem par em: {item.strip()}")
        if not norm(kw):
            raise UserError("Falta a palavra antes do -[...]. Ex: /add switch 2 -[case]")
        entries.append((kw, excl))
    return entries


def fmt_entry(kw: str, excl: list[str]) -> str:
    return kw + (f" -[{', '.join(excl)}]" if excl else "")


def parse_link(link: str) -> tuple[str, str] | None:
    """-> ("user", nome) | ("invite", hash) | ("cid", id) | None"""
    link = link.strip()
    if link.startswith("@") and len(link) > 1:
        return "user", link[1:]
    m = LINK_RE.match(link)
    return (m.lastgroup, m[m.lastgroup]) if m else None


def split_groups(arg: str) -> tuple[str, list[str]]:
    """'ssd, rtx #pc' -> ('ssd, rtx', ['pc'])"""
    groups = list(dict.fromkeys(norm(g) for g in GROUP_RE.findall(arg)))
    return GROUP_RE.sub(" ", arg).strip(), groups


def label(group: str) -> str:
    return GERAL if group == GERAL else f"#{group}"


def albums(msgs):
    """Agrupa mensagens consecutivas do mesmo álbum (ordem: mais antiga primeiro)."""
    batch = []
    for m in msgs:
        if batch and (not m.grouped_id or m.grouped_id != batch[-1].grouped_id):
            yield batch
            batch = []
        batch.append(m)
    if batch:
        yield batch


def post_link(chat, msg_id: int) -> str:
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{msg_id}"
    return f"https://t.me/c/{chat.id}/{msg_id}"


# ---------- estado ----------
class State:
    def __init__(self, data: dict):
        # grupo -> {palavra: [exclusões]}. Migra formatos antigos:
        # só "keywords" (sem grupos) vira o geral; grupo como lista vira dict sem exclusões
        raw = data.get("groups") or {GERAL: data.get("keywords", [])}
        self.groups: dict[str, dict[str, list[str]]] = {
            g: v if isinstance(v, dict) else {k: [] for k in v} for g, v in raw.items()
        }
        self.groups.setdefault(GERAL, {})
        # apelido -> {id, title, link, groups, last_id (último post já checado)}
        self.channels: dict[str, dict] = data.get("channels", {})
        for c in self.channels.values():
            c.setdefault("groups", [])
        self.relay: int | None = data.get("relay")
        self.started: bool = data.get("started", False)
        self.owner: int | None = data.get("owner")  # seu id; salvo pra avisar mesmo se o login cair

    @classmethod
    def load(cls) -> "State":
        return cls(json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {})

    def save(self) -> None:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(vars(self), ensure_ascii=False, indent=2))
        os.replace(tmp, STATE_FILE)

    def watched(self) -> set[int]:
        return {c["id"] for c in self.channels.values()}

    def keywords_for(self, chat_id: int) -> list[tuple[str, list[str]]]:
        c = next((c for c in self.channels.values() if c["id"] == chat_id), None)
        if not c:
            return []
        return [kv for g in [GERAL, *c["groups"]] for kv in self.groups.get(g, {}).items()]

    def channel(self, alias: str) -> dict:
        c = self.channels.get(alias.lower())
        if not c:
            raise UserError(f"Nenhum canal com apelido '{alias}'. Veja /list")
        return c

    def group(self, name: str) -> dict[str, list[str]]:
        if name not in self.groups:
            raise UserError(f"Grupo #{name} não existe. Crie com /add <palavras> #{name}")
        return self.groups[name]


# ---------- config ----------
def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.strip().startswith("#"):
                env[k.strip()] = v.strip()
    return env


def write_env(env: dict) -> None:
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(f"{k}={v}\n" for k, v in env.items())


# ---------- app ----------
class App:
    def __init__(self, env: dict):
        api_id, api_hash = int(env["TG_API_ID"]), env["TG_API_HASH"]
        self.bot_token = env["TG_BOT_TOKEN"]
        self.user = TelegramClient(str(BASE / "sessao_usuario"), api_id, api_hash)
        self.bot = TelegramClient(str(BASE / "sessao_bot"), api_id, api_hash)
        self.state = State.load()
        self.logged_out = False
        self.ready = asyncio.Event()

        from_relay = lambda e: e.chat_id == self.state.relay
        self.bot.add_event_handler(self.on_relay, events.NewMessage(func=lambda e: from_relay(e) and not e.grouped_id))
        self.bot.add_event_handler(self.on_relay_album, events.Album(func=from_relay))
        self.bot.add_event_handler(self.on_command, events.NewMessage(incoming=True, func=lambda e: e.is_private))

    async def prepare(self) -> None:
        self.state.owner = (await self.user.get_me()).id
        self.state.save()
        bot_me = await self.bot.get_me()
        await self.ensure_relay(bot_me.username)
        if not self.state.started:
            # bot só pode mandar mensagem pra quem já falou com ele
            await self.user.send_message(bot_me.username, "/start")
            self.state.started = True
            self.state.save()
        await self.bot(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code="",
            commands=[types.BotCommand(c, d) for c, d in COMMANDS],
        ))
        self.ready.set()

    async def ensure_relay(self, bot_username: str) -> None:
        if self.state.relay:
            try:
                ent = await self.user.get_entity(self.state.relay)
                if isinstance(ent, types.Channel) and not ent.left:
                    return
            except (ValueError, errors.RPCError):
                pass
            log.warning("Canal de apoio sumiu; criando outro")
        res = await self.user(functions.channels.CreateChannelRequest(
            title=RELAY_TITLE, about="Canal de apoio do promo-bot. Não apague.", broadcast=True,
        ))
        ch = res.chats[0]
        await self.user(functions.channels.EditAdminRequest(
            ch, bot_username,
            types.ChatAdminRights(post_messages=True, edit_messages=True, delete_messages=True),
            rank="bot",
        ))
        await self.user(functions.account.UpdateNotifySettingsRequest(
            types.InputNotifyPeer(await self.user.get_input_entity(ch)),
            types.InputPeerNotifySettings(mute_until=2**31 - 1),
        ))
        await self.user.edit_folder(ch, 1)  # arquiva
        self.state.relay = utils.get_peer_id(ch)
        self.state.save()

    # --- conta: checa posts novos dos canais vigiados ---
    async def poll_loop(self) -> None:
        slow_until = 0.0
        while True:
            try:
                await self.poll()
            except errors.FloodWaitError as err:
                log.warning("FLOOD_WAIT de %ss; checando mais devagar por um tempo", err.seconds)
                slow_until = time.monotonic() + err.seconds + SLOWDOWN_SECONDS
                await asyncio.sleep(err.seconds)
            except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError):
                # sai; o systemd reinicia e run() entra no modo "deslogado", que te avisa
                log.error("Login da conta caiu")
                return
            except Exception:
                log.exception("Erro ao checar canais")
            factor = 2 if time.monotonic() < slow_until else 1
            await asyncio.sleep(random.uniform(POLL_MIN, POLL_MAX) * factor)

    async def poll(self) -> None:
        chans = list(self.state.channels.values())
        if not chans:
            return
        # 1 consulta pra todos: id do post mais recente de cada canal
        peers = [types.InputDialogPeer(await self.user.get_input_entity(c["id"])) for c in chans]
        res = await self.user(functions.messages.GetPeerDialogsRequest(peers))
        tops = {utils.get_peer_id(d.peer): d.top_message for d in res.dialogs}
        for c in chans:
            top, last = tops.get(c["id"]), c.get("last_id")
            if top is None or (last is not None and top <= last):
                continue
            if last is None:  # canal novo: começa a partir do post atual
                c["last_id"] = top
                self.state.save()
                continue
            msgs = await self.user.get_messages(c["id"], min_id=last, limit=100)
            posts = [m for m in reversed(msgs) if isinstance(m, types.Message)]
            for batch in albums(posts):
                try:
                    await self.relay(c["id"], batch, "\n".join(m.raw_text or "" for m in batch))
                except errors.FloodWaitError:
                    raise
                except Exception:
                    log.exception("Erro ao encaminhar post %s de %s", batch[0].id, c["title"])
                c["last_id"] = batch[-1].id
                self.state.save()
            c["last_id"] = max(c["last_id"], top)
            self.state.save()

    async def relay(self, chat_id: int, msgs, text: str) -> None:
        await self.ready.wait()
        found = matching(self.state.keywords_for(chat_id), text or "")
        if not found:
            return
        chat = await msgs[0].get_chat()
        log.info("Bateu %s em %s", found, chat.title)
        try:
            await self.user.forward_messages(self.state.relay, msgs)
        except errors.ChatForwardsRestrictedError:
            body = text if len(text) < 3500 else text[:3500] + "…"
            await self.bot.send_message(
                self.state.owner, f"🔒 {chat.title} (não deixa encaminhar)\n\n{body}\n\n🔗 {post_link(chat, msgs[0].id)}"
            )

    # --- bot: canal de apoio -> você ---
    async def on_relay(self, e):
        await self.deliver(e, [e.message])

    async def on_relay_album(self, e):
        await self.deliver(e, e.messages)

    async def deliver(self, e, msgs) -> None:
        await self.ready.wait()
        await self.bot.forward_messages(self.state.owner, msgs)
        await self.bot.delete_messages(await e.get_input_chat(), msgs)

    # --- bot: comandos ---
    async def on_command(self, e):
        if self.logged_out:
            if e.sender_id == self.state.owner:
                await e.reply(LOGGED_OUT)
            return
        await self.ready.wait()
        if e.sender_id != self.state.owner:
            return
        m = CMD_RE.match(e.raw_text or "")
        cmd, arg = (m["cmd"].lower(), (m["arg"] or "").strip()) if m else ("help", "")
        handler = {
            "add": self.cmd_add,
            "rm": self.cmd_rm,
            "rmgroup": self.cmd_rmgroup,
            "watch": self.cmd_watch,
            "unwatch": self.cmd_unwatch,
            "link": self.cmd_link,
            "unlink": self.cmd_unlink,
            "list": self.cmd_list,
        }.get(cmd)
        try:
            reply = await handler(arg) if handler else HELP
        except UserError as err:
            reply = str(err)
        await e.reply(reply, link_preview=False)

    @staticmethod
    def entries_and_group(arg: str, usage: str) -> tuple[list[tuple[str, list[str]]], str]:
        rest, groups = split_groups(arg)
        if len(groups) > 1:
            raise UserError("Use um #grupo por vez.")
        entries = parse_entries(rest)
        if not entries:
            raise UserError(usage)
        return entries, groups[0] if groups else GERAL

    @staticmethod
    def find(items, name: str) -> str | None:
        """Item já salvo com o mesmo nome normalizado (ignora maiúscula/acento)."""
        return next((k for k in items if norm(k) == norm(name)), None)

    async def cmd_add(self, arg: str) -> str:
        entries, g = self.entries_and_group(
            arg, "Uso: /add <palavras> [#grupo]\nEx: /add ssd, rtx 4060 #pc\nEx: /add switch 2 -[case, capa]"
        )
        is_new = g not in self.state.groups
        kws = self.state.groups.setdefault(g, {})
        lines = []
        for kw, excl in entries:
            key = self.find(kws, kw)
            created = key is None
            if created:
                key = kw
                kws[key] = []
            new_excl = []
            for x in excl:
                if not self.find(kws[key], x):
                    kws[key].append(x)
                    new_excl.append(x)
            if created:
                lines.append(f"✅ Vigiando em {label(g)}: {fmt_entry(key, kws[key])}")
            elif new_excl:
                lines.append(f"✏️ Atualizada em {label(g)}: {fmt_entry(key, kws[key])}")
            else:
                lines.append(f"Já estava em {label(g)}: {fmt_entry(key, kws[key])}")
        self.state.save()
        if is_new:
            lines.append(f"Grupo novo. Ligue a canais com /link <apelido> #{g}")
        return "\n".join(lines)

    async def cmd_rm(self, arg: str) -> str:
        entries, g = self.entries_and_group(arg, "Uso: /rm <palavras> [#grupo]\nEx: /rm switch 2 -[capa]")
        kws = self.state.group(g)
        lines = []
        for kw, excl in entries:
            key = self.find(kws, kw)
            if key is None:
                lines.append(f"Não achei em {label(g)}: {kw}")
            elif excl:  # tira só as exclusões
                missing = []
                for x in excl:
                    hit = self.find(kws[key], x)
                    if hit:
                        kws[key].remove(hit)
                    else:
                        missing.append(x)
                lines.append(f"✏️ Atualizada em {label(g)}: {fmt_entry(key, kws[key])}")
                if missing:
                    lines.append(f"Ela não ignorava: {', '.join(missing)}")
            else:
                lines.append(f"🗑 Removida de {label(g)}: {fmt_entry(key, kws.pop(key))}")
        self.state.save()
        return "\n".join(lines)

    async def cmd_rmgroup(self, arg: str) -> str:
        name = norm(arg.strip().lstrip("#"))
        if not name:
            raise UserError("Uso: /rmgroup #grupo")
        if name == GERAL:
            raise UserError("O geral não pode ser apagado. Pra esvaziar, use /rm.")
        kws = self.state.group(name)
        del self.state.groups[name]
        unlinked = [a for a, c in self.state.channels.items() if name in c["groups"]]
        for a in unlinked:
            self.state.channels[a]["groups"].remove(name)
        self.state.save()
        msg = f"🗑 Grupo #{name} apagado ({len(kws)} palavra{'s' if len(kws) != 1 else ''})"
        return msg + (f"\nDesligado de: {', '.join(unlinked)}" if unlinked else "")

    async def cmd_watch(self, arg: str) -> str:
        rest, groups = split_groups(arg)
        parts = rest.split()
        if len(parts) != 2:
            raise UserError("Uso: /watch <link do canal> <apelido> [#grupos]\nEx: /watch https://t.me/canal hardware #pc")
        link, alias = parts[0], parts[1].lower()
        if not ALIAS_RE.fullmatch(alias):
            raise UserError("Apelido só pode ter letras, números, _ e -")
        if alias in self.state.channels:
            raise UserError(f"Apelido '{alias}' já usado.")
        groups = [g for g in groups if g != GERAL]
        for g in groups:
            self.state.group(g)
        ent = await self.resolve_channel(link)
        pid = utils.get_peer_id(ent)
        for a, c in self.state.channels.items():
            if c["id"] == pid:
                raise UserError(f"Esse canal já é vigiado como '{a}'.")
        self.state.channels[alias] = {"id": pid, "title": ent.title, "link": link, "groups": groups}
        self.state.save()
        return f"✅ Vigiando {ent.title} como '{alias}'\nLê: {self.reads(alias)}"

    async def cmd_unwatch(self, arg: str) -> str:
        alias = arg.strip().lower()
        if not alias:
            raise UserError("Uso: /unwatch <apelido>")
        c = self.state.channel(alias)
        del self.state.channels[alias]
        self.state.save()
        return f"🗑 Parei de vigiar {c['title']} ({alias})"

    def alias_and_groups(self, arg: str, usage: str) -> tuple[str, dict, list[str]]:
        rest, groups = split_groups(arg)
        if len(rest.split()) != 1 or not groups:
            raise UserError(usage)
        alias = rest.lower()
        return alias, self.state.channel(alias), groups

    async def cmd_link(self, arg: str) -> str:
        alias, c, groups = self.alias_and_groups(arg, "Uso: /link <apelido> #grupo\nEx: /link hardware #pc")
        for g in groups:
            self.state.group(g)
        c["groups"] += [g for g in groups if g != GERAL and g not in c["groups"]]
        self.state.save()
        return f"🔗 {alias} lê: {self.reads(alias)}"

    async def cmd_unlink(self, arg: str) -> str:
        alias, c, groups = self.alias_and_groups(arg, "Uso: /unlink <apelido> #grupo")
        if GERAL in groups:
            raise UserError("Todo canal lê o geral; não dá pra desligar.")
        c["groups"] = [g for g in c["groups"] if g not in groups]
        self.state.save()
        return f"🔗 {alias} lê: {self.reads(alias)}"

    def reads(self, alias: str) -> str:
        return ", ".join(label(g) for g in [GERAL, *self.state.channels[alias]["groups"]])

    async def cmd_list(self, _arg: str) -> str:
        names = [GERAL, *sorted(g for g in self.state.groups if g != GERAL)]
        blocks = []
        for g in names:
            kws = "\n".join(f"• {fmt_entry(k, ex)}" for k, ex in self.state.groups[g].items()) or "(vazio)"
            blocks.append(f"{label(g)}{' (todos os canais)' if g == GERAL else ''}:\n{kws}")
        chs = "\n".join(
            f"• {a}: {c['title']} — {self.reads(a)}" for a, c in self.state.channels.items()
        ) or "(nenhum — use /watch)"
        return "🔑 Palavras\n" + "\n\n".join(blocks) + f"\n\n📺 Canais\n{chs}"

    async def resolve_channel(self, link: str):
        parsed = parse_link(link)
        if not parsed:
            raise UserError("Link inválido. Ex: https://t.me/canal, @canal ou https://t.me/+convite")
        kind, val = parsed
        try:
            if kind == "invite":
                try:
                    ent = (await self.user(functions.messages.ImportChatInviteRequest(val))).chats[0]
                except errors.UserAlreadyParticipantError:
                    ent = (await self.user(functions.messages.CheckChatInviteRequest(val))).chat
            elif kind == "cid":
                ent = await self.user.get_entity(types.PeerChannel(int(val)))
            else:
                ent = await self.user.get_entity(val)
                if isinstance(ent, types.Channel) and ent.left:
                    await self.user(functions.channels.JoinChannelRequest(ent))
        except errors.InviteRequestSentError:
            raise UserError("Canal exige aprovação. Pedido enviado; quando aprovarem, mande o /watch de novo.")
        except (ValueError, errors.RPCError) as err:
            log.warning("Falha ao resolver %s: %r", link, err)
            raise UserError(f"Não consegui acessar esse canal ({type(err).__name__}).")
        if not isinstance(ent, types.Channel):
            raise UserError("Isso não é um canal.")
        return ent


# ---------- entrada ----------
async def run() -> int:
    env = load_env()
    if not all(env.get(k) for k in ("TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN")):
        log.error("Faltam credenciais em %s. Rode: venv/bin/python promo_bot.py setup", ENV_FILE)
        return 1
    app = App(env)
    await app.user.connect()
    await app.bot.start(bot_token=app.bot_token)
    if not await app.user.is_user_authorized():
        # fica só com o bot no ar, avisando; o setup reinicia o serviço quando relogar
        log.error("Conta não logada. Rode: venv/bin/python promo_bot.py setup")
        app.logged_out = True
        if app.state.owner:
            await app.bot.send_message(app.state.owner, LOGGED_OUT)
        await app.bot.run_until_disconnected()
        return 1
    await app.prepare()
    log.info("Rodando: %d grupos, %d canais", len(app.state.groups), len(app.state.channels))
    await asyncio.wait(
        [asyncio.ensure_future(c.run_until_disconnected()) for c in (app.user, app.bot)]
        + [asyncio.ensure_future(app.poll_loop())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    log.error("Desconectou; saindo pro systemd reiniciar")
    return 1


async def setup() -> int:
    subprocess.run(["systemctl", "--user", "stop", SERVICE], stderr=subprocess.DEVNULL)
    env = load_env()
    print("Configuração do promo-bot. Cole cada valor e aperte Enter.\n")
    while not env.get("TG_API_ID", "").isdigit():
        env["TG_API_ID"] = input("api_id (my.telegram.org): ").strip()
    while not env.get("TG_API_HASH"):
        env["TG_API_HASH"] = input("api_hash (my.telegram.org): ").strip()
    while ":" not in env.get("TG_BOT_TOKEN", ""):
        env["TG_BOT_TOKEN"] = input("token do bot (@BotFather): ").strip()
    write_env(env)

    app = App(env)
    print("\nLogin na sua conta do Telegram:")
    try:
        await app.user.start(
            phone=lambda: input("Seu telefone (ex: +5511999998888): "),
            code_callback=lambda: input("Código que chegou no app do Telegram: "),
            password=lambda: getpass.getpass("Senha da verificação em duas etapas: "),
        )
    except errors.ApiIdInvalidError:
        write_env({k: v for k, v in env.items() if k not in ("TG_API_ID", "TG_API_HASH")})
        print("\n❌ api_id/api_hash inválidos. Rode o setup de novo.")
        return 1
    try:
        await app.bot.start(bot_token=env["TG_BOT_TOKEN"])
    except errors.AccessTokenInvalidError:
        write_env({k: v for k, v in env.items() if k != "TG_BOT_TOKEN"})
        print("\n❌ Token do bot inválido. Rode o setup de novo.")
        return 1
    me, bot_me = await app.user.get_me(), await app.bot.get_me()
    await app.user.disconnect()
    await app.bot.disconnect()
    print(f"\n✅ Conta: {me.first_name} | bot: @{bot_me.username}")

    ok = subprocess.run(["systemctl", "--user", "enable", SERVICE]).returncode == 0
    ok = ok and subprocess.run(["systemctl", "--user", "restart", SERVICE]).returncode == 0
    print("✅ Rodando. Abra o chat com o bot no Telegram e mande /help." if ok
          else "⚠️ Login ok, mas não consegui iniciar o serviço.")
    return 0 if ok else 1


if __name__ == "__main__":
    os.umask(0o077)  # sessões e estado só legíveis pelo dono
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    sys.exit(asyncio.run(setup() if sys.argv[1:] == ["setup"] else run()))
