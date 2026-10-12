"""Yes24 제공 지식 데이터(메모) CLI — 팀 대리 적재·로컬 실험 서버 적재(design/memo-model.md 9절).

어드민과 **같은** `read_import`·`validate_*`·`MemoService`를 부른다(규칙이 한 벌이다). 접속은
`SESSION_DB_URL`이며, 시작할 때 대상 backend/host/db를 stderr에 먼저 찍는다. MySQL이면 변경을
`admin_audit`에 actor NULL·`after.by="cli"`로 같은 트랜잭션에 남기고, sqlite(로컬)는 감사 테이블이
없어 남기지 않는다. 메모 단건 추가·편집은 어드민의 몫이라 두지 않는다.

`--collection`·묶음 인자는 숫자면 id, 아니면 이름 완전 일치다(이름은 유일하다). 되돌릴 수 없는 일
(delete-collection, 켜진 묶음의 --replace)은 대상을 보여 준 뒤 `--yes`가 있어야 한다.

  uv run python scripts/memos.py init [--print]
  uv run python scripts/memos.py collections
  uv run python scripts/memos.py create-collection --title T --description-file D.txt \\
      [--source-url U] [--period 2026-04-01:2026-05-17]
  uv run python scripts/memos.py import FILE --collection ID|이름 [--sheet S] --content 열 \\
      [--content 열]... [--about 열] [--goods 열] [--link 열] [--replace] [--dry-run] [--yes]
  uv run python scripts/memos.py show --collection ID|이름 [--query Q]
  uv run python scripts/memos.py activate|deactivate ID|이름
  uv run python scripts/memos.py pin|unpin ID|이름   # 항상 참고(전문을 매 턴 지시문에)
  uv run python scripts/memos.py delete-collection ID|이름 --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yes24_agent.admin_auth import AUDIT_INSERT, audit_params  # noqa: E402
from yes24_agent.config import get_settings  # noqa: E402
from yes24_agent.memos import (  # noqa: E402
    CollectionMeta,
    ImportMapping,
    get_memo_service,
    mysql_ddl,
    read_import,
    search_view,
)

_CLI = "cli"


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _audit():
    """MySQL만 감사 — admin_accounts.py의 `after.by="cli"` 관례."""
    if make_url(get_settings().session_db_url).get_backend_name() != "mysql":
        return None
    return lambda action, cid, before, after: (
        AUDIT_INSERT,
        audit_params(None, "memo_collection", cid, action, before, {**(after or {}), "by": _CLI}),
    )


def _period(value: str | None) -> tuple[date | None, date | None]:
    if not value:
        return None, None
    start, _, end = value.partition(":")
    return (date.fromisoformat(start) if start else None, date.fromisoformat(end) if end else None)


async def _resolve(service, ref: str) -> dict:
    """숫자 = id, 그 밖 = 이름 완전 일치(이름은 UNIQUE라 여러 개가 맞을 수 없다)."""
    key = "id" if ref.isdigit() else "title"
    value = int(ref) if key == "id" else ref
    found = [item for item in await service.list_collections() if item[key] == value]
    if not found:
        raise HTTPException(status_code=404, detail=f"없는 묶음입니다: {ref}")
    return found[0]


def _confirmed(args, collection: dict, what: str) -> bool:
    if args.yes:
        return True
    state = "답변에 사용 중" if collection["active"] else "꺼짐"
    _warn(
        f"{what} 대상: #{collection['id']} {collection['title']} · 메모 "
        f"{collection['memo_count']}개 · {state}\n"
        "되돌릴 수 없습니다. 진행하려면 --yes를 붙여 다시 실행하세요."
    )
    return False


async def _import(args, service) -> int:
    path = Path(args.file)
    mapping = ImportMapping(args.content, args.about, args.goods, args.link)
    result = read_import(path.name, path.read_bytes(), sheet=args.sheet, mapping=mapping,
                         settings=get_settings())
    if args.dry_run or result.errors:
        _print(asdict(result))
        return 1 if result.errors else 0
    collection = await _resolve(service, args.collection)
    if args.replace and collection["active"] and not _confirmed(
        args, collection, f"켜진 묶음 통째 교체(지금 {collection['memo_count']}개 → 새 "
        f"{len(result.memos)}개)"
    ):
        return 1
    mode = "replace" if args.replace else "append"
    saved = await service.import_memos(
        collection["id"], result, mode, collection["revision"], _CLI, _audit()
    )
    _print({**saved, "skipped_rows": result.skipped_rows, "notes": result.notes})
    return 0


async def run(args) -> int:
    service = get_memo_service()
    try:
        if args.command == "init":
            if args.print:
                print(mysql_ddl())
            else:
                await service.init_schema()
                _warn("memo_collections·memos 준비됨")
        elif args.command == "collections":
            _print(await service.list_collections())
        elif args.command == "create-collection":
            start, end = _period(args.period)
            meta = CollectionMeta(
                args.title, Path(args.description_file).read_text(encoding="utf-8"),
                args.source_url, start, end,
            )
            _print(await service.create_collection(meta, _CLI, _audit()))
        elif args.command == "import":
            return await _import(args, service)
        elif args.command == "show":
            collection = await _resolve(service, args.collection)
            listing = await service.list_memos(collection["id"], args.query)
            _print(search_view(listing["memos"], get_settings()))
        elif args.command == "delete-collection":
            collection = await _resolve(service, args.collection)
            if not _confirmed(args, collection, "묶음 삭제"):
                return 1
            await service.delete_collection(
                collection["id"], collection["revision"], _CLI, _audit()
            )
            _warn(f"#{collection['id']} {collection['title']} 삭제됨")
        else:  # activate | deactivate | pin | unpin
            collection = await _resolve(service, args.collection)
            key = "active" if args.command.endswith("activate") else "always_include"
            _print(await service.update_collection(
                collection["id"], {key: args.command in ("activate", "pin")},
                collection["revision"], _CLI, _audit(),
            ))
    except HTTPException as exc:
        detail = getattr(exc, "current", None) or exc.detail
        _warn(f"실패({exc.status_code}): {json.dumps(detail, ensure_ascii=False)}")
        return 1
    finally:
        await service.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="테이블 생성(멱등)").add_argument(
        "--print", action="store_true", help="MySQL DDL만 출력"
    )
    sub.add_parser("collections", help="묶음 목록(꺼진 것 포함)")
    create = sub.add_parser("create-collection", help="새 묶음(꺼진 상태)")
    create.add_argument("--title", required=True)
    create.add_argument(
        "--description-file", required=True,
        help="설명 파일(모델이 언제 이 묶음을 쓸지 읽는 신호). 틀: 언제 쓰는 자료: … / "
        "누가 만들었나: … / 자료 한 건 = … / 주의(기간·대상): …",
    )
    create.add_argument("--source-url")
    create.add_argument("--period", help="YYYY-MM-DD:YYYY-MM-DD(한쪽 생략 가능)")
    imp = sub.add_parser("import", help="엑셀/CSV → 메모(묶음에 추가 또는 --replace 통째 교체)")
    imp.add_argument("file")
    imp.add_argument("--collection", required=True)
    imp.add_argument("--sheet", help="읽을 시트(기본 첫 시트)")
    imp.add_argument("--content", action="append", required=True,
                     help="내용 열(열마다 한 번 — 여러 열이면 '열이름: 값' 줄로 이어 붙인다)")
    imp.add_argument("--about", help="누가 열")
    imp.add_argument("--goods", help="상품번호 열")
    imp.add_argument("--link", help="링크 열(Yes24 주소 — 카드가 이 페이지로 간다)")
    imp.add_argument("--replace", action="store_true")
    imp.add_argument("--dry-run", action="store_true", help="저장하지 않고 결과·오류 출력")
    imp.add_argument("--yes", action="store_true", help="켜진 묶음 교체 확인")
    show = sub.add_parser("show", help="에이전트가 볼 응답(조회 도구와 같은 투영)")
    show.add_argument("--collection", required=True)
    show.add_argument("--query", default="")
    for command in ("activate", "deactivate", "pin", "unpin"):
        sub.add_parser(command).add_argument("collection")
    remove = sub.add_parser(
        "delete-collection", help="되돌릴 수 없다 — 답변에서만 빼려면 deactivate"
    )
    remove.add_argument("collection")
    remove.add_argument("--yes", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    url = make_url(get_settings().session_db_url)
    _warn(f"대상: {url.get_backend_name()} host={url.host} db={url.database}")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
