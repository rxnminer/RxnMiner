import hashlib, importlib.util, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
S=ROOT/'.agents/skills/annotation-review/scripts'
def mod(name):
 spec=importlib.util.spec_from_file_location(name,S/f'{name}.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
prep=mod('prepare_blind_review_inputs');agree=mod('compute_overlap_agreement')
def dump(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,indent=2)+'\n')
def base_v2():return {'reaction_templates':[],'selected_examples':[],'reaction_graph':[]}
def fixture(tmp_path):
 r=tmp_path/'pkg';pid='case-a';snap=r/'snapshot/pages'/pid;snap.mkdir(parents=True);(snap/'source.png').write_bytes(b'png');dump(snap/'accepted_revision.patch.json',{'mask_regions':[]});mat=r/'materialized-human'/pid;mat.mkdir(parents=True);dump(mat/'gt.json',base_v2());pi=r/'pi-sealed'/pid;pi.mkdir(parents=True);dump(pi/'pred.v2.json',base_v2());skills=r/'protocol/skills/annotation-review';skills.mkdir(parents=True);(skills/'SKILL.md').write_text('x');(r/'protocol/REVIEW_PROTOCOL.md').write_text('x');row={'page_id':pid,'snapshot_page':f'snapshot/pages/{pid}','materialized_case':f'materialized-human/{pid}','pi_status':'available','assignment':'overlap10'};m=r/'manifest.jsonl';m.write_text(json.dumps(row)+'\n');return r,m,pid
def test_prepare_pass_a_excludes_pi(tmp_path,monkeypatch):
 r,m,pid=fixture(tmp_path);monkeypatch.setattr('sys.argv',['x','--package-root',str(r),'--manifest',str(m),'--reviewer','alice','--pass','A','--out',str(tmp_path/'a')]);assert prep.main()==0;c=tmp_path/'a/cases'/pid;assert (c/'candidate.v2.json').is_file();t=json.loads((c/'TASK.json').read_text());assert t['lane']=='human_settled' and t['forbidden_lanes']==['pi_sealed']
def test_prepare_pass_b_uses_pi(tmp_path,monkeypatch):
 r,m,pid=fixture(tmp_path);monkeypatch.setattr('sys.argv',['x','--package-root',str(r),'--manifest',str(m),'--reviewer','alice','--pass','B','--out',str(tmp_path/'b')]);assert prep.main()==0;t=json.loads((tmp_path/'b/cases'/pid/'TASK.json').read_text());assert t['lane']=='pi_sealed' and t['candidate_sha256']==hashlib.sha256((r/'pi-sealed'/pid/'pred.v2.json').read_bytes()).hexdigest()
def review(root,pid,value):
 d=root/pid;d.mkdir(parents=True);dump(d/'SEAL.review.json',{'schema':'chemrxn.source-review-seal.v1'});dump(d/'review.json',{'findings':[{'target_type':'molecule','target_key':'selected_examples.0.products.0','connectivity':value,'stereo':'correct','membership':'correct','condition':'not_applicable','metric':'not_applicable','action':'keep'}]})
def test_overlap_agreement(tmp_path,monkeypatch):
 pid='case-a';m=tmp_path/'o.jsonl';m.write_text(json.dumps({'page_id':pid})+'\n');review(tmp_path/'a',pid,'correct');review(tmp_path/'b',pid,'incorrect');out=tmp_path/'report.json';monkeypatch.setattr('sys.argv',['x','--overlap-manifest',str(m),'--review-a',str(tmp_path/'a'),'--review-b',str(tmp_path/'b'),'--out',str(out)]);assert agree.main()==0;r=json.loads(out.read_text());assert r['dimensions']['connectivity']['raw_agreement']==0 and len(r['disagreements'])==1
