from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import structlog
import typer

from pc_price_tracker import db as db_module
from pc_price_tracker.builder import find_best_build
from pc_price_tracker.compat import load_performance_tiers
from pc_price_tracker.constants import CATEGORIES
from pc_price_tracker.models import RawOffer
from pc_price_tracker.normalize import NormalizationError, normalize_offer
from pc_price_tracker.sources import SOURCES, SourceBlocked

app = typer.Typer(add_completion=False, help="Парсер цен на ПК-комплектующие для контент-конвейера")
log = structlog.get_logger()

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "pc_price_tracker.db"


def _ingest(conn: sqlite3.Connection, raw_offers: list[RawOffer]) -> tuple[int, int]:
    ok = failed = 0
    for raw in raw_offers:
        try:
            normalized = normalize_offer(raw)
        except NormalizationError as exc:
            db_module.insert_unmatched(conn, raw, reason=str(exc))
            failed += 1
            continue
        product_id = db_module.upsert_product(conn, normalized)
        db_module.insert_offer(conn, product_id, raw)
        ok += 1
    conn.commit()
    return ok, failed


class _CheckpointIngest:
    """Persists each unit of work the moment a source finishes it.

    Portfolio hard rule, and the reason it exists: a long scrape that keeps
    everything in memory until one final save loses ALL of it when the
    process dies — which has actually happened here (a mid-run machine
    shutdown, and an external process kill that stopped a scraper silently
    after 8 of 11 units). A source calls BaseSource._checkpoint() after each
    catalog page; this ingests and COMMITS that page immediately, so a death
    on page 7 still leaves pages 1-6 durably in the database.

    It also tracks what it has already written, so the tail-ingest below
    can't double-count offers a checkpointing source already persisted —
    which keeps this safe for sources that checkpoint partially or not at
    all.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.ok = 0
        self.failed = 0
        self.total = 0
        self.checkpoints = 0
        self._seen: set[tuple[str, str]] = set()

    def __call__(self, offers: list[RawOffer]) -> None:
        pending = [o for o in offers if (o.source, o.external_id) not in self._seen]
        if not pending:
            return
        ok, failed = _ingest(self.conn, pending)
        for offer in pending:
            self._seen.add((offer.source, offer.external_id))
        self.ok += ok
        self.failed += failed
        self.total += len(pending)
        self.checkpoints += 1

    def finish(self, raw_offers: list[RawOffer]) -> None:
        """Ingest whatever the source returned but never checkpointed."""
        self(raw_offers)


def _validate_category(category: str) -> None:
    if category not in CATEGORIES:
        typer.echo(f"Неизвестная категория {category!r}. Доступные: {', '.join(CATEGORIES)}", err=True)
        raise typer.Exit(1)


def _validate_source(source: str) -> None:
    if source not in SOURCES:
        typer.echo(f"Неизвестный источник {source!r}. Доступные: {', '.join(SOURCES)}", err=True)
        raise typer.Exit(1)


@app.command()
def scrape(
    source: str = typer.Option(..., "--source", help="Источник, например regard"),
    category: str = typer.Option(..., "--category", help="Категория: " + ", ".join(CATEGORIES)),
    max_pages: int | None = typer.Option(
        None, "--max-pages", help="Ограничить число страниц каталога (для небольших проверочных прогонов)"
    ),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
) -> None:
    """Один прогон: один источник, одна категория."""
    _validate_source(source)
    _validate_category(category)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db_module.connect(db_path)
    captured_at = datetime.now()
    sink = _CheckpointIngest(conn)
    src = SOURCES[source](offer_sink=sink, max_pages=max_pages)
    try:
        raw_offers = src.fetch_category(category, captured_at)
    except SourceBlocked as exc:
        # Whatever was checkpointed before the block is already committed —
        # report it rather than implying the whole run was lost.
        log.error("source_blocked", source=source, category=category, error=str(exc))
        typer.echo(f"Источник {source} остановлен: {exc}", err=True)
        if sink.total:
            typer.echo(f"Сохранено до остановки: {sink.total} офферов ({sink.ok} нормализовано)", err=True)
        raise typer.Exit(1) from exc

    sink.finish(raw_offers)
    typer.echo(f"{source}/{category}: {sink.total} офферов, {sink.ok} нормализовано, {sink.failed} в unmatched")


@app.command("scrape-all")
def scrape_all(db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db")) -> None:
    """Полный обход всех источников и категорий."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db_module.connect(db_path)
    captured_at = datetime.now()

    total_ok = total_failed = 0
    for source_name, source_cls in SOURCES.items():
        # One instance per source, as before: it caches that site's
        # robots.txt for the run, so building a fresh one per category would
        # re-fetch robots.txt eight times over for no benefit.
        src = source_cls()
        for category in CATEGORIES:
            # One sink per (source, category): that pairing is the unit of
            # work this loop already recovers from, and a fresh sink keeps
            # each unit's reported numbers its own.
            sink = _CheckpointIngest(conn)
            src.offer_sink = sink
            try:
                raw_offers = src.fetch_category(category, captured_at)
            except SourceBlocked as exc:
                log.error("source_blocked", source=source_name, category=category, error=str(exc))
                note = f" (сохранено до остановки: {sink.total})" if sink.total else ""
                typer.echo(f"[{source_name}/{category}] остановлено: {exc}{note}", err=True)
                total_ok += sink.ok
                total_failed += sink.failed
                continue
            sink.finish(raw_offers)
            total_ok += sink.ok
            total_failed += sink.failed
            typer.echo(f"[{source_name}/{category}] {sink.total} офферов, {sink.ok} ок, {sink.failed} unmatched")

    typer.echo(f"Готово: {total_ok} офферов нормализовано, {total_failed} в unmatched")


@app.command()
def build(
    budget: int = typer.Option(..., "--budget", help="Бюджет в рублях"),
    profile: str = typer.Option("gaming", "--profile", help="gaming, workstation, student, office"),
    include_preorder: bool = typer.Option(
        False,
        "--include-preorder",
        help="Игнорировать require_availability (data/build_rules.yaml) — собрать из чего угодно, включая предзаказ и «нет в наличии»",
    ),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
) -> None:
    """Собрать конфигурацию под бюджет и профиль."""
    conn = db_module.connect(db_path)
    offers_by_category = {
        category: db_module.cheapest_offer_per_product(conn, category, in_stock_only=False) for category in CATEGORIES
    }

    try:
        result = find_best_build(offers_by_category, budget, profile, include_preorder=include_preorder)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Профиль: {result.profile}, бюджет: {budget} ₽")
    if result.notional_budget is not None and result.notional_budget != budget:
        typer.echo(f"Коридоры посчитаны от notional-бюджета {result.notional_budget} ₽ (реально потрачено не больше {budget} ₽)")

    if result.untiered_skipped:
        parts = ", ".join(f"{cat}: {n}" for cat, n in result.untiered_skipped.items())
        typer.echo(f"Без tier (пропущены, см. лог): {parts}")

    if result.floor_unrecognized:
        parts = ", ".join(f"{cat}: {n}" for cat, n in result.floor_unrecognized.items())
        typer.echo(f"Floor из build_rules.yaml пропущен — параметр не извлёкся из данных (не отбраковано): {parts}")

    if result.brand_rejected:
        parts = ", ".join(f"{cat}: {n}" for cat, n in result.brand_rejected.items())
        typer.echo(f"Вне whitelist бренда / не прошли требования (data/brand_rules.yaml): {parts}")

    if result.brand_rule_unrecognized:
        parts = ", ".join(f"{cat}: {n}" for cat, n in result.brand_rule_unrecognized.items())
        typer.echo(f"Правило brand_rules.yaml пропущено — параметр не извлёкся из данных (не отбраковано): {parts}")

    if result.availability_rejected:
        parts = ", ".join(f"{cat}: {n}" for cat, n in result.availability_rejected.items())
        typer.echo(f"Отфильтровано по доступности (data/build_rules.yaml: require_availability): {parts}")

    if result.availability_unrecognized:
        parts = ", ".join(f"{cat}: {n}" for cat, n in result.availability_unrecognized.items())
        typer.echo(f"Статус доступности не определён — не отбраковано: {parts}")

    if not result.feasible:
        typer.echo("-" * 70)
        typer.echo("Не удалось собрать конфигурацию под этот бюджет и профиль.")
        typer.echo(f"Причина: {result.infeasible_reason}")
        if result.minimum_budget_estimate is not None:
            typer.echo(f"Минимальный бюджет для профиля «{profile}» с текущими данными: ~{result.minimum_budget_estimate} ₽")
        if result.warnings:
            typer.echo("Предупреждения:")
            for warning in result.warnings:
                typer.echo(f"  - {warning}")
        raise typer.Exit(1)

    typer.echo("-" * 70)
    for item in result.items:
        typer.echo(f"{item.category:12s} {item.brand} {item.model}")
        typer.echo(f"             {item.price} ₽  [{item.source}]  {item.url}")
        typer.echo(f"             -> {item.reason}")
    typer.echo("-" * 70)
    typer.echo(f"Итого: {result.total_price} ₽ (бюджет {budget} ₽, в рамках бюджета)")

    if result.compatibility_issues:
        typer.echo("Проблемы совместимости:")
        for issue in result.compatibility_issues:
            typer.echo(f"  - {issue}")
    if result.warnings:
        typer.echo("Предупреждения:")
        for warning in result.warnings:
            typer.echo(f"  - {warning}")


@app.command()
def tiers(
    unverified: bool = typer.Option(False, "--unverified", help="Только source=estimated"),
    top: int = typer.Option(20, "--top", help="Сколько позиций показать"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
) -> None:
    """Записи data/performance_tiers.yaml, отсортированные по числу офферов
    в базе — чтобы приоритизировать ручную сверку самых ходовых позиций."""
    tiers_data = load_performance_tiers()
    conn = db_module.connect(db_path)
    product_info = db_module.offer_counts_by_product(conn)

    rows = []
    for key, entry in tiers_data.items():
        if unverified and entry.get("source") != "estimated":
            continue
        info = product_info.get(key, {"category": "?", "brand": "?", "model": key, "offer_count": 0})
        rows.append((info["offer_count"], key, entry, info))

    rows.sort(key=lambda r: r[0], reverse=True)

    if not rows:
        typer.echo("Нечего показывать (нет tier-записей, соответствующих фильтру).")
        return

    for offer_count, key, entry, info in rows[:top]:
        typer.echo(
            f"{offer_count:4d} офферов  tier {entry['tier']:3d} ({entry['source']:9s})  "
            f"{info['brand']} {info['model']}  [{key}]"
        )
    typer.echo(f"Показано {min(top, len(rows))} из {len(rows)}")


@app.command()
def movers(
    days: int = typer.Option(7, "--days", help="Окно в днях"),
    limit: int = typer.Option(20, "--limit"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
) -> None:
    """Товары с наибольшим изменением цены за N дней."""
    conn = db_module.connect(db_path)
    results = db_module.movers(conn, days)[:limit]
    if not results:
        typer.echo("Нет данных за указанный период (нужно минимум два прогона scrape).")
        return
    for r in results:
        arrow = "↑" if r["delta"] > 0 else "↓"
        typer.echo(
            f"{arrow} {r['category']:12s} {r['brand']} {r['model'][:40]:40s} "
            f"{r['old_price']} -> {r['new_price']} ₽ ({r['pct']:+.1f}%) [{r['source']}]"
        )


@app.command()
def compare(
    category: str = typer.Option(..., "--category", help="Категория: " + ", ".join(CATEGORIES)),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
) -> None:
    """Сравнить цены на одинаковые товары между источниками (кросс-магазинное сравнение)."""
    _validate_category(category)
    conn = db_module.connect(db_path)
    offers = db_module.latest_offers(conn, category=category, in_stock_only=True)

    by_product: dict[int, list[dict]] = defaultdict(list)
    for offer in offers:
        by_product[offer["product_id"]].append(offer)

    comparable = {pid: group for pid, group in by_product.items() if len({o["source"] for o in group}) >= 2}

    sources_seen = sorted({o["source"] for o in offers})
    if not comparable:
        typer.echo(
            f"В категории «{category}» нет товаров, которые встречаются в двух и более источниках "
            f"одновременно (сейчас есть данные из: {', '.join(sources_seen) if sources_seen else 'ни одного источника'})."
        )
        return

    typer.echo(f"Категория «{category}»: {len(comparable)} товаров с ценами из нескольких источников")
    typer.echo("-" * 70)
    for group in comparable.values():
        group_sorted = sorted(group, key=lambda o: o["price"])
        cheapest = group_sorted[0]
        typer.echo(f"{group[0]['brand']} {group[0]['model']}")
        for offer in group_sorted:
            if offer is cheapest:
                typer.echo(f"  [{offer['source']:10s}] {offer['price']} ₽  (дешевле всех)  {offer['url']}")
            else:
                delta = offer["price"] - cheapest["price"]
                pct = (delta / cheapest["price"] * 100) if cheapest["price"] else 0.0
                typer.echo(f"  [{offer['source']:10s}] {offer['price']} ₽  +{delta} ₽ (+{pct:.1f}%)  {offer['url']}")


@app.command()
def health(
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
    backup_dir: Path = typer.Option(Path("./backups"), "--backup-dir"),
    log_dir: Path = typer.Option(Path("./logs"), "--log-dir"),
) -> None:
    """Последний успешный прогон, глубина истории, размер последнего бэкапа."""
    conn = db_module.connect(db_path)
    last_capture = conn.execute("SELECT MAX(captured_at) FROM offers").fetchone()[0]
    first_capture = conn.execute("SELECT MIN(captured_at) FROM offers").fetchone()[0]

    typer.echo("=== pc-price-tracker: health ===")

    if last_capture:
        last_dt = datetime.fromisoformat(last_capture)
        age = datetime.now() - last_dt
        age_str = f"{age.days} дн. {age.seconds // 3600} ч. назад" if age.days else f"{age.seconds // 3600} ч. {(age.seconds % 3600) // 60} мин. назад"
        typer.echo(f"Последний захват данных в БД: {last_capture} ({age_str})")
    else:
        typer.echo("Данных в БД ещё нет — не было ни одного успешного scrape.")

    if first_capture and last_capture:
        days_span = (datetime.fromisoformat(last_capture) - datetime.fromisoformat(first_capture)).days
        typer.echo(f"Глубина истории: {days_span} дн. (с {first_capture[:10]} по {last_capture[:10]})")

    # Rotated daily — check all scrape_all.log* files, most recent first,
    # for the last plan run's outcome (this is the scheduled-task-specific
    # signal; last_capture above already covers "is the data itself fresh"
    # regardless of whether a run was manual or scheduled).
    log_files = sorted(log_dir.glob("scrape_all.log*"), key=lambda p: p.stat().st_mtime, reverse=True) if log_dir.exists() else []
    if log_files:
        last_success_line = None
        last_failure_line = None
        for log_file in log_files:
            text = log_file.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if "succeeded (exit 0)" in line:
                    last_success_line = line
                elif "attempts failed" in line:
                    last_failure_line = line
            if last_success_line or last_failure_line:
                break
        if last_success_line:
            typer.echo(f"Последний успешный плановый прогон (по логу): {last_success_line.split('[', 1)[0].strip()}")
        elif last_failure_line:
            typer.echo(f"ВНИМАНИЕ: последний плановый прогон провалился после всех попыток: {last_failure_line.split('[', 1)[0].strip()}")
        else:
            typer.echo("В логах планового прогона нет ни одной завершённой попытки.")
    else:
        typer.echo(f"Лог планового прогона не найден в {log_dir} (задача ещё не запускалась или логи не настроены).")

    backups = sorted(backup_dir.glob("pc_price_tracker_*.db"), key=lambda p: p.name, reverse=True) if backup_dir.exists() else []
    if backups:
        latest = backups[0]
        size_kb = latest.stat().st_size / 1024
        typer.echo(f"Последний бэкап: {latest.name} ({size_kb:.0f} КБ, всего копий: {len(backups)})")
    else:
        typer.echo(f"Бэкапов не найдено в {backup_dir}.")


@app.command()
def backup(
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
    backup_dir: Path = typer.Option(Path("./backups"), "--backup-dir"),
    keep: int = typer.Option(30, "--keep", help="Сколько последних копий хранить"),
) -> None:
    """Снять снапшот SQLite в --backup-dir и удалить копии сверх --keep.

    История цен — единственный невосполнимый актив проекта, поэтому бэкап
    использует sqlite3 Connection.backup() (а не копирование файла), чтобы
    корректно выгрузить базу даже если она открыта другим процессом
    (например, тем же scrape-all, если бэкап запущен параллельно)."""
    if not db_path.exists():
        typer.echo(f"База {db_path} не найдена — нечего бэкапить", err=True)
        raise typer.Exit(1)

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"pc_price_tracker_{timestamp}.db"

    src_conn = sqlite3.connect(db_path)
    dest_conn = sqlite3.connect(dest)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()

    typer.echo(f"Бэкап сохранён: {dest} ({dest.stat().st_size / 1024:.0f} КБ)")

    existing = sorted(backup_dir.glob("pc_price_tracker_*.db"), key=lambda p: p.name, reverse=True)
    to_delete = existing[keep:]
    for old in to_delete:
        old.unlink()
    if to_delete:
        typer.echo(f"Удалено старых копий: {len(to_delete)} (храним последние {keep})")


@app.command()
def export(
    out: Path = typer.Option(Path("./out"), "--out"),
    export_format: str = typer.Option("json", "--format"),
    days: int = typer.Option(7, "--days", help="Окно для списка изменений цен"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
) -> None:
    """Подготовить данные для генерации сценария: актуальные цены + движения."""
    if export_format != "json":
        typer.echo(f"Формат {export_format!r} не поддерживается, доступен только json", err=True)
        raise typer.Exit(1)

    conn = db_module.connect(db_path)
    out.mkdir(parents=True, exist_ok=True)

    products = []
    for category in CATEGORIES:
        products.extend(db_module.cheapest_offer_per_product(conn, category))
    movers_list = db_module.movers(conn, days)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "products": products,
        "movers": movers_list,
    }
    out_path = out / "export.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    typer.echo(f"Экспортировано {len(products)} позиций и {len(movers_list)} изменений цен в {out_path}")


if __name__ == "__main__":
    app()
