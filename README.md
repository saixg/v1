# CodeAxis

**From Codebase to Creation.**

CodeAxis turns a repository into a navigable software model — then uses that model to
give AI coding agents real context, and to plan new work on top of code that already
exists.

> ### ⚠️ Status: Refining phase
>
> CodeAxis is being actively refined and is **not yet a general-availability product**.
> The architecture, data model and analysis pipelines are in place; the surfaces on top
> of them are being hardened, re-measured and tightened repository by repository.
>
> Expect rough edges, changing interfaces, and features that are intentionally labelled
> *partial* rather than shipped. **This README will be replaced with a full product and
> setup guide once the working build is ready.** Watch the repository for that update.

---

## The problem

AI coding agents are good at writing code and bad at knowing where it goes.

Drop an agent into a repository of any real size and it guesses: which module owns this
behaviour, which function is the one actually called in production, which constraint will
break if you touch the auth boundary. Most tools answer those questions by handing a model
a pile of files and hoping. The answers sound right and cite paths that don't exist.

CodeAxis takes the opposite position: **facts about a codebase come from analysing the
codebase.** Parsers, resolvers and graph analysis produce the structure. Models only
interpret, explain and propose — they are never the source of a file path, a symbol, or a
relationship that gets stored as fact.

---

## How it works
