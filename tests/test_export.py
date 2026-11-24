from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from cik_export import BASE, Collector, HttpClient, decode_report, Progress, retry_after, catalog, find_region, export_excel, validate_report
from argparse import Namespace
from unittest.mock import Mock, patch
from io import StringIO
from openpyxl import load_workbook


class FidelityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = json.loads((BASE / 'tests/fixtures/protocols.json').read_text(encoding='utf-8'))

    def test_accepts_official_zero(self):
        r = validate_report(deepcopy(self.records[0]['report']))
        self.assertEqual(r['rows'][10]['count'], '0')

    def test_rejects_obfuscated_count(self):
        report = deepcopy(self.records[0]['report'])
        report['rows'][0]['count'] = 'а2ф.г8еы%'
        with self.assertRaisesRegex(ValueError, 'Нечисловое'):
            validate_report(report)

    def test_rejects_reordered_protocol(self):
        report = deepcopy(self.records[0]['report'])
        report['rows'][0], report['rows'][1] = report['rows'][1], report['rows'][0]
        with self.assertRaisesRegex(ValueError, 'порядок'):
            validate_report(report)

    def test_keeps_source_percent_even_when_not_recomputed(self):
        records = deepcopy(self.records)
        for rec in records:
            rec['report']['rows'][12]['percent'] = '12.34%'
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            output = Path(tmp) / 'check.xlsx'
            export_excel(records, output)
            wb = load_workbook(output)
            self.assertEqual(wb.worksheets[0]['B17'].value, 12.34)

    def test_all_cells_match_source(self):
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            output = Path(tmp) / 'check.xlsx'
            export_excel(self.records, output)
            wb = load_workbook(output)
            overview = [r for r in self.records if r['node']['type'] in (3, 4)]
            tik = next(r for r in self.records if r['node']['type'] == 4)
            self.assertEqual(wb.sheetnames, ['ОИК','ТИК для проверки','УИК'])
            groups = [(wb.worksheets[i], [r for r in self.records if r['node']['type']==level]) for i,level in enumerate((3,4,5))]
            for sheet, group in groups:
                self.assertEqual(sheet.cell(39,1).value, 'Протокол подписан')
                self.assertEqual(sheet.cell(40,1).value, 'Скачано (UTC)')
                self.assertNotIn('Опубликовано', [cell.value for row in sheet for cell in row])
                for col, rec in enumerate(group, 2):
                    for i, row in enumerate(rec['report']['rows']):
                        target = 4+i if i<12 else 16+2*(i-12)
                        self.assertEqual(sheet.cell(target, col).value, int(row['count']))
                        if row['percent'] is not None:
                            self.assertAlmostEqual(sheet.cell(target+1, col).value, float(Decimal(row['percent'].strip('%'))))
                    self.assertEqual(sheet.cell(38,col).hyperlink.target, rec['report']['url'])
                    self.assertEqual(sheet.cell(39,col).value, rec['report']['signed_at'] or 'Не указано на сайте')
            self.assertEqual(len(wb.worksheets[1].merged_cells.ranges), 10)
            self.assertEqual(wb.worksheets[1].freeze_panes, 'B4')

    def test_catalog_and_region_aliases(self):
        c = catalog()
        self.assertEqual(len(c),225)
        self.assertEqual(c[3][0],c[8][0])
        self.assertNotEqual(c[3][0],c[9][0])
        node={'type':2,'name':'город Москва'}
        self.assertEqual(find_region({'x':node},'г. Москва'),node)

    def test_region_config_rejects_duplicate_oik(self):
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            path=Path(tmp)/'regions.json'
            path.write_text(json.dumps({'Регион А':[1],'Регион Б':[1]}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'повторяющийся'):
                catalog(path)

    def test_export_uses_internal_config(self):
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            output=Path(tmp)/'result.xlsx'
            export_excel(self.records,output)
            result=load_workbook(output)
            self.assertEqual(result['ОИК']['B1'].value,'ОИК №225')
            self.assertEqual(result.sheetnames,['ОИК','ТИК для проверки','УИК'])

    def test_merged_headers_with_two_oiks(self):
        records=[]
        for number in (3,4):
            group=deepcopy(self.records)
            for r in group:
                for n in [r['node']]+r['ancestors']:
                    n['externalId'] += str(number)
                    if n['type']==3:
                        n['number']=number
                r['report']['url'] += '&test='+str(number)
            records.extend(group)
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            output=Path(tmp)/'merged.xlsx'
            export_excel(records,output)
            w=load_workbook(output)
            self.assertEqual(w['ОИК']['B1'].value,'ОИК №3')
            self.assertEqual(w['ОИК']['C1'].value,'ОИК №4')
            merges={str(x) for x in w['УИК'].merged_cells}
            self.assertTrue({'B1:C1','D1:E1','B2:C2','D2:E2'} <= merges)

    def test_collector_covers_all_oiks_of_selected_region(self):
        # Reproduce two OIKs under one region without downloading another region.
        nodes={'region':{'id':1,'externalId':'region','name':'Республика Башкортостан','type':2,
                         'children':[{'externalId':'o3','number':3,'type':3},{'externalId':'o4','number':4,'type':3}]}}
        for number in (3,4):
            nodes['o'+str(number)]={'externalId':'o'+str(number),'name':'ОИК','number':number,'type':3,'hasChildren':False}
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            folder=Path(tmp)
            (folder/'tree.json').write_text(json.dumps(nodes),encoding='utf-8')
            args=Namespace(region='Республика Башкортостан',directory={3:('Республика Башкортостан','ОИК №3'),4:('Республика Башкортостан','ОИК №4')},
                           refresh=False,limit=0)
            collector=Collector(Mock(),args,folder)
            collector.discover=Mock()
            collector.read=lambda key,path:{'id':key}
            result=collector.collect()
            self.assertEqual([r['id'] for r in result],['o3','o4'])

    def test_http_tree_bootstrap_selects_requested_region(self):
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            args=Namespace(region='Республика Башкортостан',directory={3:('Республика Башкортостан','ОИК №3')},
                           refresh=False,limit=0)
            http=Mock()
            http.get_json.return_value={'type':0,'externalId':'root','children':[
                {'type':2,'externalId':'other','name':'Ямало-Ненецкий автономный округ'},
                {'type':2,'externalId':'wanted','name':args.region,'children':[
                    {'type':3,'externalId':'o3','number':3,'hasChildren':False}]}]}
            collector=Collector(http,args,Path(tmp))
            collector.read=lambda key,path:{'id':key}
            self.assertEqual(collector.collect(),[{'id':'o3'}])
            self.assertEqual(http.get_json.call_count,1)

    def test_full_plan_exists_before_any_protocol_even_with_limit(self):
        nodes={
            'region':{'type':2,'externalId':'region','name':'Республика Башкортостан','children':[{'externalId':'o','number':3,'type':3}]},
            'o':{'type':3,'externalId':'o','children':[{'externalId':'t'}]},
            't':{'type':4,'externalId':'t','children':[{'externalId':'u'}]},
            'u':{'type':5,'externalId':'u','hasChildren':False}}
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            folder=Path(tmp)
            (folder/'tree.json').write_text(json.dumps(nodes),encoding='utf-8')
            args=Namespace(url='http://izbirkom.ru/election/1/commission/start/results',region='Республика Башкортостан',
                           directory={3:('Республика Башкортостан','ОИК №3')},refresh=False,limit=1,timeout=1)
            c=Collector(Mock(),args,folder)
            discovered=[]
            c.discover=lambda key:discovered.append(key)
            def read(key,path):
                plan=json.loads((folder/'plan.json').read_text(encoding='utf-8'))
                self.assertEqual([x['id'] for x in plan],['o','t','u'])
                self.assertEqual(discovered,['region','o','t','u'])
                return {'id':key}
            c.read=read
            self.assertEqual(c.collect(),[{'id':'o'}])

    def test_http_backoff_retry_after(self):
        import requests
        self.assertEqual(retry_after('120'),120)
        self.assertEqual(retry_after('invalid'),0)
        session=Mock(headers={})
        def response(status,headers=None):
            r=Mock(status_code=status,headers=headers or {})
            r.json.return_value={'ok':True}
            if status>=400:r.raise_for_status.side_effect=requests.HTTPError()
            return r
        session.request.side_effect=[response(429),response(503,{'Retry-After':'120'}),response(200)]
        clock=[0]
        def sleep(seconds):clock[0]+=seconds
        client=HttpClient(session=session)
        client.key='test'
        with patch('cik_export.time.monotonic',side_effect=lambda:clock[0]),patch('cik_export.time.sleep',side_effect=sleep):
            self.assertEqual(client.get_json('http://izbirkom.ru/test'),{'ok':True})
        self.assertGreaterEqual(clock[0],150)

    def test_three_requests_per_second_counts_response_time(self):
        session=Mock(headers={})
        clock=[0.0]
        starts=[]
        response=Mock(status_code=200,headers={})
        response.json.return_value={'ok':True}
        def request(*args,**kwargs):
            starts.append(clock[0])
            clock[0]+=0.1
            return response
        session.request.side_effect=request
        def sleep(seconds):clock[0]+=seconds
        client=HttpClient(session=session)
        client.key='test'
        with patch('cik_export.time.monotonic',side_effect=lambda:clock[0]),patch('cik_export.time.sleep',side_effect=sleep):
            for _ in range(4):client.get_json('http://izbirkom.ru/test')
        for i,start in enumerate(starts):self.assertAlmostEqual(start,i/3)

    def test_slow_response_does_not_add_extra_rate_pause(self):
        session=Mock(headers={})
        clock=[0.0]
        starts=[]
        response=Mock(status_code=200,headers={})
        response.json.return_value={}
        def request(*args,**kwargs):
            starts.append(clock[0])
            clock[0]+=0.8
            return response
        session.request.side_effect=request
        client=HttpClient(session=session)
        client.key='test'
        with patch('cik_export.time.monotonic',side_effect=lambda:clock[0]),patch('cik_export.time.sleep') as sleep:
            for _ in range(3):client.get_json('http://izbirkom.ru/test')
        self.assertEqual(starts,[0.0,0.8,1.6])
        sleep.assert_not_called()

    def test_service_key_renewal_without_javascript_execution(self):
        client=HttpClient(session=Mock(headers={}))
        client.json=Mock(side_effect=[{'pubToken':'temporary','jsTask':'return 12+30;'}, {'apiKey':'test'}])
        client.authorize()
        self.assertEqual(client.key,'test')
        self.assertEqual(client.json.call_args.kwargs['json']['answer'],'42')
        client.json=Mock(return_value={'pubToken':'temporary','jsTask':'return dangerous();'})
        with self.assertRaises(ValueError):client.authorize()

    def test_raw_api_matches_browser_fixture(self):
        raw=json.loads((BASE/'tests/fixtures/raw_oik.json').read_text(encoding='utf-8'))
        old=self.records[0]
        report=decode_report(raw,old['node']['externalId'],old['report']['url'])
        self.assertEqual(report['rows'],old['report']['rows'])
        with self.assertRaises(ValueError):decode_report(raw,'different-commission','url')
        raw['body']['protocolNum']=1
        with self.assertRaises(ValueError):decode_report(raw,old['node']['externalId'],'url')

    def test_progress_color_and_plain_fallback(self):
        stream=StringIO()
        stream.isatty=lambda:True
        with patch('cik_export.pink_supported',return_value=True),patch.dict('os.environ',{'TERM':'xterm'}):
            p=Progress(1,stream)
            p.update(5)
        self.assertIn('\033[95m',stream.getvalue())
        stream=StringIO()
        p=Progress(1,stream,plain=True)
        p.update(5,False)
        self.assertNotIn('\033[',stream.getvalue())
        self.assertIn('ошибок 1',stream.getvalue())


if __name__ == '__main__':
    unittest.main()
