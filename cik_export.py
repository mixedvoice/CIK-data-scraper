"""Официальные итоги федерального бюллетеня: ОИК → ТИК → УИК.

Запуск с автоматической установкой зависимостей: python run.py
Прямые HTTP-запросы к публичному API, браузер не используется.
"""
from __future__ import annotations

import argparse
from copy import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import math
import os
import shutil
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
if (BASE / '.deps').is_dir():
    sys.path.insert(0, str(BASE / '.deps'))

DEFAULT_URL = 'http://izbirkom.ru/election/587813923/commission/d16f979c-e2e1-44db-86af-d06997e62948/results?type=6&report=242'
LOG = logging.getLogger('cik')
SUCCESS = 25
logging.addLevelName(SUCCESS,'SUCCESS')


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def validate_report(report):
    """Never turn a missing/obfuscated cell into zero or calculate a percentage."""
    if report['heading'] != 'Итоги голосования по федеральному избирательному округу':
        raise ValueError('Открыт другой отчёт: ' + report['heading'])
    rows = report['rows']
    if not rows:
        raise ValueError('Пустой протокол')
    for i, row in enumerate(rows, 1):
        if row['number'] != str(i):
            raise ValueError(f'Нарушен порядок строк: {row["number"]}, ожидалось {i}')
        count = re.sub(r'[\s\u00a0\u202f]', '', row['count'])
        if not re.fullmatch(r'\d+', count):
            raise ValueError(f'Нечисловое значение в строке {i}: {row["count"]}')
        row['count'] = count
        if row['percent'] is not None:
            if not re.fullmatch(r'\d+(?:[.,]\d+)?\s*%', row['percent']):
                raise ValueError(f'Не прочитан процент в строке {i}: {row["percent"]}')
            p = Decimal(row['percent'].replace('%', '').replace(',', '.').strip())
            if not 0 <= p <= 100:
                raise ValueError(f'Процент вне диапазона: {p}')
    return report


def retry_after(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def pink_supported(stream):
    if not stream.isatty() or os.environ.get('NO_COLOR'):
        return False
    if os.name != 'nt':
        return os.environ.get('TERM') != 'dumb'
    try:
        import ctypes
        import msvcrt
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
        mode = ctypes.c_ulong()
        api = ctypes.windll.kernel32
        return bool(api.GetConsoleMode(handle, ctypes.byref(mode)) and
                    api.SetConsoleMode(handle, mode.value | 4))
    except (OSError, ValueError, AttributeError):
        return False


class ConsoleFormatter(logging.Formatter):
    def __init__(self, color=False):
        super().__init__('%(asctime)s %(levelname)s %(message)s')
        self.color = color

    def format(self, record):
        record = copy(record)
        tag = f'[{record.levelname}]'
        if self.color:
            if record.levelno >= logging.ERROR:
                tag = f'\033[91m{tag}\033[0m'
            elif record.levelno == logging.WARNING:
                tag = f'\033[93m{tag}\033[0m'
            elif record.levelno == SUCCESS:
                tag = f'\033[92m{tag}\033[0m'
        record.levelname = tag
        return super().format(record)


class ConsoleHandler(logging.StreamHandler):
    def emit(self, record):
        progress = Progress.active
        if progress is not None and progress.stream is self.stream:
            self.stream.write('\r'+' '*progress.previous_width+'\r')
            progress.previous_width = 0
        super().emit(record)
        if progress is not None and progress.stream is self.stream:
            progress.update()


def configure_logging():
    handler = ConsoleHandler()
    handler.setFormatter(ConsoleFormatter(pink_supported(handler.stream)))
    logging.basicConfig(level=logging.INFO,handlers=[handler],force=True)


class Progress:
    active = None

    def __init__(self, total, stream=None, plain=False):
        self.total, self.stream = total, stream or sys.stderr
        self.color = not plain and pink_supported(self.stream)
        self.done, self.failed = Counter(), 0
        self.previous_width = 0
        try:
            '█░'.encode(self.stream.encoding or 'utf-8')
            self.graphical = self.stream.isatty() and os.environ.get('TERM') != 'dumb'
        except (UnicodeEncodeError, LookupError):
            self.graphical = False

    def update(self, level=None, success=True):
        if level is not None:
            if success:
                self.done[level] += 1
            else:
                self.failed += 1
        completed = sum(self.done.values())+self.failed
        if not self.stream.isatty() and completed % 10 and completed != self.total:
            return
        percent = 100*completed/max(1,self.total)
        error = f'ошибок {self.failed}'
        if self.color:
            error = f'\033[91m{error}\033[0m'
        info = (f'{percent:5.1f}% | {completed} из {self.total} | ОИК {self.done[3]} | '
                f'ТИК {self.done[4]} | УИК {self.done[5]} | {error}')
        line = info
        columns = shutil.get_terminal_size((100,20)).columns
        if self.graphical:
            info_width = len(re.sub(r'\x1b\[[0-9;]*m','',info))
            width = max(4,min(28,columns-info_width-3))
            full = int(width*min(1,completed/max(1,self.total)))
            filled = '█'*full
            track = '░'*(width-len(filled))
            bar = f'\033[95m{filled}\033[90m{track}\033[0m' if self.color else filled+track
            line = f'{bar} | {info}'
        visible_width = len(re.sub(r'\x1b\[[0-9;]*m','',line))
        padding = ' '*max(0,self.previous_width-visible_width) if self.stream.isatty() else ''
        self.previous_width = visible_width
        self.stream.write(('\r' if self.stream.isatty() else '')+line+padding+
                          ('\n' if not self.stream.isatty() or completed == self.total else ''))
        self.stream.flush()
        if self.stream.isatty():
            Progress.active = self if completed < self.total else None

API = 'http://apps.cikrf.ru/service/ik-inp-service-pbcopy'
HEADING = 'Итоги голосования по федеральному избирательному округу'


class HttpClient:
    def __init__(self, rps=3, timeout=45, session=None):
        import requests
        if not math.isfinite(rps) or rps <= 0:
            raise ValueError('Частота запросов должна быть положительным конечным числом')
        self.session = session or requests.Session()
        self.session.headers['User-Agent'] = 'CIK-Results-Exporter/1.0'
        self.interval, self.timeout = 1/rps, timeout
        self.key, self.next_request, self.overloads = None, 0, 0

    def close(self):
        self.session.close()

    def wait(self):
        while time.monotonic() < self.next_request:
            time.sleep(max(0,min(1,self.next_request-time.monotonic())))

    def authorize(self):
        task = self.json('GET',API+'/challenge/get',auth=False)
        match = re.fullmatch(r'\s*return\s+(\d+)\s*\+\s*(\d+)\s*;\s*',task['jsTask'])
        if not match:
            raise ValueError('Неизвестный формат служебной задачи API; выполнение JavaScript запрещено')
        result = self.json('POST',API+'/challenge/solve',auth=False,json={
            'pubToken':task['pubToken'],'answer':str(int(match[1])+int(match[2])),
            'fingerprint':self.session.headers['User-Agent']})
        self.key = result['apiKey']
        self.session.headers.update({'X-Api-Key':self.key,'X-Client-Fingerprint':self.session.headers['User-Agent']})

    def json(self, method, url, auth=True, **kwargs):
        import requests
        if auth and not self.key:
            self.authorize()
        for attempt in range(3):
            self.wait()
            self.next_request = time.monotonic()+self.interval
            try:
                response = self.session.request(method,url,timeout=self.timeout,**kwargs)
                if response.status_code == 401 and auth and attempt < 2:
                    self.authorize()
                    continue
                if response.status_code in (429,503):
                    seconds = max(retry_after(response.headers.get('Retry-After')),min(300,30*2**min(self.overloads,4)))
                    self.overloads += 1
                    self.next_request = max(self.next_request,time.monotonic()+seconds)
                    LOG.warning('HTTP %s: пауза не менее %.0f секунд',response.status_code,seconds)
                    if attempt < 2:
                        continue
                response.raise_for_status()
                return response.json()
            except (requests.RequestException,ValueError) as exc:
                if attempt == 2:
                    raise
                LOG.warning('Запрос %s, попытка %s/3: %s',urlsplit(url).path,attempt+1,type(exc).__name__)
                self.next_request = max(self.next_request,time.monotonic()+2**attempt)
        raise RuntimeError('Не получен ответ API')

    def get_json(self, url, params=None):
        return self.json('GET',url,params=params)


def decode_report(raw, key, url):
    if not isinstance(raw,dict) or raw.get('reportType') != '242':
        raise ValueError('API не вернул протокол 242')
    body = raw['body']
    if body.get('commissionClassifierId') != key or body.get('protocolNum') != 2:
        raise ValueError('Получен протокол другой комиссии или другого бюллетеня')
    records = {}
    for row in body['records']:
        number = int(row['infoPrintNum'])
        item = {'number':str(number),'label':row['infoText'].strip(),'count':str(row['value']),
                'percent':str(row['percentValue'])+'%' if row.get('percentValue') is not None else None}
        if number in records and records[number] != item:
            raise ValueError('Противоречащие друг другу строки протокола')
        records[number] = item
    signed = body.get('signDateTime') if body.get('signed') else None
    report = {'rows':[records[n] for n in sorted(records)],'url':url,'heading':HEADING,
              'signed_at':datetime.fromisoformat(signed).strftime('%d.%m.%Y %H:%M:%S') if signed else '',
              'report_created_at':raw.get('createdAt')}
    return validate_report(report)


class Collector:
    def __init__(self, http, args, folder):
        self.http, self.args, self.folder = http, args, folder
        self.nodes = {}

    def remember(self, node):
        if not isinstance(node,dict) or not node.get('externalId'):
            raise ValueError('Неизвестная структура дерева комиссий')
        key = node['externalId']
        self.nodes[key] = {**self.nodes.get(key,{}),**node}
        for child in node.get('children',[]):
            self.remember(child)

    def url(self, key):
        return f'{self.origin}/election/{self.election}/commission/{key}/results?type=6&report=242'

    def discover(self, key=None):
        node = self.nodes.get(key,{})
        if key and ('children' in node or node.get('type') == 5 or node.get('hasChildren') is False):
            return
        if key:
            level = {2:'ИКСРФ',3:'ОИК',4:'ТИК',5:'УИК'}.get(node.get('type'),'комиссия')
            LOG.info('Дерево: %s — %s',level,node.get('name',key))
        params = {'electionsId':self.election}
        if key:
            params['classifierId'] = key
        data = self.http.get_json(API+'/commissionClassifiers',params)
        if key and data.get('externalId') != key:
            raise ValueError('API вернул другую ветвь дерева')
        self.remember(data)

    def read(self, key, ancestors):
        cached = self.folder/'reports'/f'{key}.json'
        if cached.exists() and not self.args.refresh:
            result = json.loads(cached.read_text(encoding='utf-8'))
            validate_report(result['report'])
            if result['node']['externalId'] != key:
                raise ValueError('Кэш содержит протокол другой комиссии')
            result['ancestors'] = ancestors
            return result
        raw = self.http.get_json(API+'/reports/242',{'commissionClassifierId':key,'protocolNum':2})
        atomic_json(self.folder/'responses'/f'{key}.json',raw)
        report = decode_report(raw,key,self.url(key))
        result = {'node':{k:v for k,v in self.nodes[key].items() if k!='children'},'ancestors':ancestors,
                  'report':report,'downloaded_at':datetime.now(timezone.utc).isoformat()}
        atomic_json(cached,result)
        return result

    def collect(self):
        parsed = urlsplit(DEFAULT_URL)
        match = re.fullmatch(r'/election/(\d+)/commission/([\w-]+)/results/?', parsed.path)
        if parsed.hostname not in ('izbirkom.ru', 'www.izbirkom.ru') or not match:
            raise ValueError('Нужна ссылка izbirkom.ru/election/.../commission/.../results')
        self.origin = f'{parsed.scheme}://{parsed.netloc}'
        self.election, root = match.groups()
        treefile = self.folder/'tree.json'
        atomic_json(self.folder/'run.json',{'complete':False,'partial':bool(self.args.limit),'url':DEFAULT_URL})
        if treefile.exists() and not self.args.refresh:
            self.nodes = json.loads(treefile.read_text(encoding='utf-8'))
        if not any(n.get('type') == 2 and region_key(n['name']) == region_key(self.args.region) for n in self.nodes.values()):
            self.discover()
        region = find_region(self.nodes, self.args.region)
        self.discover(region['externalId'])
        region = self.nodes[region['externalId']]
        roots = [c for c in region.get('children', []) if c['type'] == 3]
        expected = {n for n, (r, _) in self.args.directory.items() if r == self.args.region}
        if {c['number'] for c in roots} != expected:
            raise ValueError('Список ОИК на сайте отличается от config/regions.json')
        roots.sort(key=lambda c: c['number'])
        LOG.info('Обход дерева региона: %s. Это может занять некоторое время.', self.args.region)
        plan, visited = [], set()

        def walk(key, ancestors):
            if key in visited:
                raise ValueError(f'Комиссия повторяется в дереве: {key}')
            visited.add(key)
            self.discover(key)
            node = self.nodes[key]
            if node.get('type') in (3,4):
                atomic_json(treefile,self.nodes)
            if node.get('type') not in (3,4,5):
                raise ValueError('Неизвестный тип комиссии: '+key)
            if node.get('hasChildren') and not node.get('children'):
                raise ValueError('Пустой список дочерних комиссий: '+key)
            plan.append({'id':key, 'type':node['type'], 'ancestors':ancestors})
            path = ancestors + [{k:node.get(k) for k in ('externalId','name','type','number')}]
            for child in node.get('children', []):
                walk(child['externalId'], path)

        for root_node in roots:
            walk(root_node['externalId'], [])
        atomic_json(treefile,self.nodes)
        atomic_json(self.folder/'plan.json',plan)
        counts = Counter(entry['type'] for entry in plan)
        LOG.info('%s: %s ОИК, %s ТИК, %s УИК',self.args.region,counts[3],counts[4],counts[5])
        selected = plan[:self.args.limit] if self.args.limit else plan
        progress = Progress(len(selected), plain=getattr(self.args,'plain_progress',False))
        progress.update()
        results, errors = [], []
        for entry in selected:
            key = entry['id']
            try:
                results.append(self.read(key,entry['ancestors']))
                progress.update(entry['type'])
            except Exception as exc:
                errors.append({'id':key,'name':self.nodes[key]['name'],'url':self.url(key),'error':str(exc)})
                progress.update(entry['type'],False)
        atomic_json(self.folder / 'errors.json', errors)
        atomic_json(self.folder / 'collected.json', results)
        if errors:
            raise RuntimeError(f'Не загружено комиссий: {len(errors)}. См. {self.folder / "errors.json"}. '
                               'Excel не создан. Повторный запуск продолжит из кэша.')
        atomic_json(self.folder / 'run.json', {'complete': True, 'partial': bool(self.args.limit),
                    'url': DEFAULT_URL, 'commissions': len(results), 'expected':dict(counts)})
        return results


def catalog(path=BASE/'config/regions.json'):
    regions = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(regions,dict) or not regions:
        raise ValueError('Справочник config/regions.json должен содержать регионы')
    result = {}
    for region, numbers in regions.items():
        if not isinstance(region,str) or not region.strip() or not isinstance(numbers,list) or not numbers:
            raise ValueError('Неверная запись региона в config/regions.json')
        for number in numbers:
            if type(number) is not int or number < 1 or number in result:
                raise ValueError(f'Неверный или повторяющийся номер ОИК в config/regions.json: {number}')
            result[number] = (region,f'ОИК №{number}')
    if not result:
        raise ValueError('Справочник config/regions.json пуст')
    return result


def region_key(name):
    name = re.sub(r'\([^)]*\)', '', name.lower()).replace('ё', 'е')
    name = re.sub(r'^г[.]?\s+', 'город ', name)
    return re.sub(r'[^а-яa-z0-9]', '', name)


def find_region(nodes, region):
    matches = [n for n in nodes.values() if n.get('type') == 2 and region_key(n['name']) == region_key(region)]
    if len(matches) != 1:
        raise ValueError(f'Не удалось однозначно найти регион на сайте: {region}')
    return matches[0]


def export_excel(records, output: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, Side
    from openpyxl.utils import get_column_letter
    directory = catalog()
    if not records or any(r['node']['type'] not in (3, 4, 5) for r in records):
        raise ValueError('Пустая выгрузка или неизвестный тип комиссии')
    rows = records[0]['report']['rows']
    if len(rows) != 22 or sum(r['percent'] is not None for r in rows) != 10:
        raise ValueError('Ожидались 12 показателей и 10 партий')
    for rec in records:
        validate_report(rec['report'])
        if [(r['number'], r['percent'] is not None) for r in rec['report']['rows']] != [(r['number'], r['percent'] is not None) for r in rows] or [r['label'] for r in rec['report']['rows'][12:]] != [r['label'] for r in rows[12:]]:
            raise ValueError('Разный порядок строк или партий: ' + rec['node']['name'])
    wb = Workbook()
    wb.active.title = 'ОИК'
    sheets = [wb.active, wb.create_sheet('ТИК для проверки'), wb.create_sheet('УИК')]
    font = Font(name='Times New Roman', size=8)
    header_font = Font(name='Times New Roman', size=10, bold=True)
    edge = Side(style='thin', color='000000')
    border = Border(left=edge, right=edge, top=edge, bottom=edge)
    for sheet, level in zip(sheets, (3, 4, 5)):
        group = [r for r in records if r['node']['type'] == level]
        for cells in sheet.iter_rows(max_row=40, max_col=len(group)+1):
            for cell in cells:
                if cell.row in (36,37):
                    continue
                cell.font = header_font if cell.row <= 3 else font
                cell.alignment = Alignment(horizontal='left' if cell.column == 1 else 'center',
                                           vertical='center', wrap_text=True)
                if cell.row <= 35:
                    cell.border = border
        for i in range(16, 36, 2):
            sheet.merge_cells(start_row=i, start_column=1, end_row=i+1, end_column=1)
            sheet.row_dimensions[i+1].height = 18
        labels = group[0]['report']['rows'] if group else rows
        for i, row in enumerate(labels):
            dest = 4+i if i < 12 else 16+2*(i-12)
            sheet.cell(dest, 1, row['label'])
            sheet.row_dimensions[dest].height = max(26, math.ceil(len(row['label'])/55)*12+5)
        for row, label in [(1,'ОИК'), (2,'ТИК'), (3,'УИК'), (38,'Источник'),
                           (39,'Протокол подписан'), (40,'Скачано (UTC)')]:
            sheet.cell(row, 1, label)
        headers = []
        for col, rec in enumerate(group, 2):
            node, path = rec['node'], rec['ancestors']
            oik = next((a for a in path if a['type']==3), node if level==3 else None)
            tik = next((a for a in path if a['type']==4), node if level==4 else None)
            if oik is None or oik['number'] not in directory:
                raise ValueError('ОИК отсутствует в config/regions.json')
            headers.append((oik['externalId'], tik['externalId'] if tik else ''))
            sheet.cell(1, col, directory[oik['number']][1])
            if tik:
                sheet.cell(2, col, tik['name'])
            if level == 5:
                sheet.cell(3, col, str(node['number']))
            for i, row in enumerate(rec['report']['rows']):
                dest = 4+i if i<12 else 16+2*(i-12)
                sheet.cell(dest, col, int(row['count'])).number_format = '0'
                if row['percent'] is not None:
                    value = float(Decimal(row['percent'].replace('%','').replace(',','.').strip()))
                    sheet.cell(dest+1, col, value).number_format = 'General'
            for row, value in [(38,rec['report']['url']),
                               (39,rec['report']['signed_at'] or 'Не указано на сайте'), (40,rec['downloaded_at'])]:
                sheet.cell(row, col, value)
            sheet.cell(38,col).value = 'Открыть отчёт'
            sheet.cell(38,col).hyperlink = rec['report']['url']
            sheet.cell(40,col).value = datetime.fromisoformat(rec['downloaded_at']).strftime('%d.%m.%Y %H:%M:%S')
            for footer in range(38,41):
                sheet.cell(footer,col).alignment = Alignment(vertical='center', wrap_text=True)
                sheet.row_dimensions[footer].height = 30
            sheet.column_dimensions[get_column_letter(col)].width = 18
        for header_row in (1, 2):
            start = 0
            while start < len(headers):
                end = start+1
                key = headers[start][:header_row]
                while end < len(headers) and headers[end][:header_row] == key:
                    end += 1
                if end-start > 1 and (header_row == 1 or headers[start][1]):
                    sheet.merge_cells(start_row=header_row, start_column=start+2, end_row=header_row, end_column=end+1)
                    sheet.cell(header_row, start+2).alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
                start = end
        sheet.column_dimensions['A'].width = 60
        for row in (1,2,3):
            sheet.row_dimensions[row].height = 30
        sheet.freeze_panes = 'B4'
        sheet.sheet_view.topLeftCell = 'A1'
        sheet.print_area = f'A1:{get_column_letter(max(2,len(group)+1))}40'
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix('.tmp.xlsx')
    wb.save(temp)
    temp.replace(output)
    LOG.log(SUCCESS,'Готово. Excel: %s', output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Имя Excel внутри папки региона')
    parser.add_argument('--oik', type=int, help='Любой номер ОИК нужного региона')
    parser.add_argument('--data-dir', type=Path, default=BASE, help='Родительская папка для папок регионов')
    parser.add_argument('--refresh', action='store_true', help='Заново скачать сохранённые протоколы')
    parser.add_argument('--plain-progress', action='store_true', help='Прогресс без цвета')
    parser.add_argument('--limit', type=int, default=0, help='Тест: ограничить число комиссий; файл получает суффикс _partial')
    parser.add_argument('--export-only', action='store_true', help='Собрать Excel из ранее скачанных данных')
    args = parser.parse_args(argv)
    configure_logging()
    try:
        args.directory = catalog()
        args.oik = args.oik or int(input('Введите номер любой ОИК вашего региона: ').strip())
        if args.oik not in args.directory:
            raise ValueError('Номер ОИК отсутствует в config/regions.json')
        args.region = args.directory[args.oik][0]
        args.data_dir = args.data_dir / args.region
        args.output = args.data_dir / (args.output.name if args.output else 'Результаты_2026.xlsx')
        if args.export_only:
            run = json.loads((args.data_dir / 'run.json').read_text(encoding='utf-8'))
            if not run['complete']:
                raise ValueError('Загрузка прервана. Сначала завершите скачивание.')
            if run['partial']:
                args.limit = args.limit or 1
            errors = args.data_dir / 'errors.json'
            if errors.exists() and json.loads(errors.read_text(encoding='utf-8')):
                raise ValueError('В выгрузке есть ошибки, сначала повторите скачивание')
            records = json.loads((args.data_dir / 'collected.json').read_text(encoding='utf-8'))
        else:
            http = HttpClient()
            try:
                records = Collector(http,args,args.data_dir).collect()
            finally:
                http.close()
        if args.limit:
            args.output = args.output.with_stem(args.output.stem + '_partial')
        export_excel(records, args.output)
        return 0
    except Exception as exc:
        LOG.error('%s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
