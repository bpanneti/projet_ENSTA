"""Évaluation hors ligne, lecture SQLite seule. Aucun import Qt.

L'origine des plots sert de référence d'association. La vérité est interpolée
linéairement en ECEF, sans extrapolation ni franchissement des trous de données.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import csv
import json
import math
import sqlite3
import numpy as np
from pyproj import Transformer
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import cdist
from scipy.stats import chi2


@dataclass
class MetricsOptions:
    truth_max_gap_s: float = 1.0
    continuity_gap_s: float = 1.5
    grid_step_s: float = .5
    confidence: float = .95
    nees_dimension: int = 2
    confirmed_only: bool = False
    include_predictions: bool = True
    last_state_per_timestamp: bool = True
    min_origin_purity: float = .6
    min_association_mass: float = .5
    compute_gospa: bool = False
    gospa_cutoff_m: float = 100.
    start_offset_s: float | None = None
    end_offset_s: float | None = None

    def validate(self):
        for name in ('truth_max_gap_s','continuity_gap_s','grid_step_s','gospa_cutoff_m'):
            if not math.isfinite(getattr(self,name)) or getattr(self,name)<=0:
                raise ValueError(f'{name} doit être positif')
        if not .5 < self.confidence < 1 or self.nees_dimension not in (2,3):
            raise ValueError('Confiance ou dimension NEES invalide')
        if not .5 < self.min_origin_purity <= 1 or not 0 <= self.min_association_mass <= 1:
            raise ValueError('Seuils d’origine invalides')
        for name in ('start_offset_s','end_offset_s'):
            value=getattr(self,name)
            if value is not None and not math.isfinite(value):
                raise ValueError('Fenêtre temporelle non finie')
        return self


def jload(value, default=None):
    try:
        return json.loads(value) if value is not None else default
    except (ValueError,TypeError):
        return default


def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError,OverflowError):
        return None


def timestamp(value):
    result=number(value)
    if result is not None:
        return result
    if isinstance(value,str):
        try:
            date=datetime.fromisoformat(value.replace('Z','+00:00'))
            return date.timestamp() if date.tzinfo is not None else None
        except ValueError:
            pass
    return None


def unwrap(value):
    while isinstance(value,(list,tuple)) and len(value)==1:
        value=value[0]
    return value


def clean_json(value):
    if isinstance(value,dict):
        return {str(k):clean_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple,np.ndarray)):
        return [clean_json(v) for v in value]
    if isinstance(value,np.generic):
        return clean_json(value.item())
    if isinstance(value,float) and not math.isfinite(value):
        return None
    return value


def merge_intervals(intervals):
    result=[]
    for start,end in sorted(intervals):
        if end < start:
            continue
        if result and start <= result[-1][1]+1e-6:
            result[-1]=(result[-1][0],max(result[-1][1],end))
        else:
            result.append((float(start),float(end)))
    return result


def duration(intervals):
    return sum(b-a for a,b in merge_intervals(intervals))


def intersect_intervals(left,right):
    left,right=merge_intervals(left),merge_intervals(right)
    i=j=0; result=[]
    while i<len(left) and j<len(right):
        a,b=max(left[i][0],right[j][0]),min(left[i][1],right[j][1])
        if a<=b:
            result.append((a,b))
        if left[i][1]<right[j][1]: i+=1
        else: j+=1
    return merge_intervals(result)


def segments(times,gap):
    values=sorted(set(float(t) for t in times))
    if not values:return []
    result=[]; start=previous=values[0]
    for t in values[1:]:
        if t-previous>gap+1e-6:
            result.append((start,previous));start=t
        previous=t
    result.append((start,previous))
    return result


class Frame:
    def __init__(self,origin):
        self.origin=np.asarray(origin,float)
        if self.origin.shape!=(3,) or not np.isfinite(self.origin).all():
            raise ValueError('Origine ENU invalide')
        transform=Transformer.from_crs(4979,4978,always_xy=True)
        self.ecef=np.array(transform.transform(*self.origin))
        lon,lat=np.radians(self.origin[:2])
        self.basis=np.array([[-np.sin(lon),np.cos(lon),0],
            [-np.sin(lat)*np.cos(lon),-np.sin(lat)*np.sin(lon),np.cos(lat)],
            [np.cos(lat)*np.cos(lon),np.cos(lat)*np.sin(lon),np.sin(lat)]])

    def local(self,ecef):return self.basis@(np.asarray(ecef)-self.ecef)
    def velocity(self,ecef_velocity):return self.basis@np.asarray(ecef_velocity)


class TruthTrajectory:
    def __init__(self,identifier,name,samples):
        self.identifier,self.name=identifier,name
        self.times=np.array([s['timestamp'] for s in samples],float)
        self.positions=np.array([s['ecef'] for s in samples],float)
        self.altitude_known=np.array([s['altitude_known'] for s in samples],bool)

    def interpolate(self,t,max_gap):
        if not len(self.times) or t<self.times[0]-1e-6 or t>self.times[-1]+1e-6:
            return None,'hors intervalle vérité'
        k=int(np.searchsorted(self.times,t))
        exact=next((i for i in (k,k-1) if 0<=i<len(self.times) and abs(self.times[i]-t)<=1e-6),None)
        if exact is not None:
            p=self.positions[exact].copy(); alt=bool(self.altitude_known[exact])
            i=exact if exact+1<len(self.times) else exact-1
            velocity=None
            if i>=0 and i+1<len(self.times):
                dt=self.times[i+1]-self.times[i]
                if 0<dt<=max_gap+1e-6:
                    velocity=(self.positions[i+1]-self.positions[i])/dt
            return {'ecef':p,'velocity_ecef':velocity,'altitude_known':alt,'gap_s':0.},None
        if not 0<k<len(self.times):return None,'hors intervalle vérité'
        i=k-1; dt=self.times[k]-self.times[i]
        if dt>max_gap+1e-6:return None,'trou dans la vérité'
        alpha=(t-self.times[i])/dt
        return {'ecef':(1-alpha)*self.positions[i]+alpha*self.positions[k],
                'velocity_ecef':(self.positions[k]-self.positions[i])/dt,
                'altitude_known':bool(self.altitude_known[i] and self.altitude_known[k]),'gap_s':float(dt)},None

    def support(self,max_gap,start,end):
        return intersect_intervals(segments(self.times,max_gap),[(start,end)])


def gospa(truth,estimates,cutoff=100.):
    """GOSPA p=2, alpha=2 ; coûts décomposés en m²."""
    a=np.asarray(truth,float);b=np.asarray(estimates,float)
    n,m=len(a),len(b); matched=0; localisation=0.
    if n and m:
        distances=cdist(a,b)
        rows,cols=linear_sum_assignment(np.minimum(distances,cutoff)**2)
        for i,j in zip(rows,cols):
            if distances[i,j]<cutoff:
                matched+=1;localisation+=float(distances[i,j]**2)
    missed,false=n-matched,m-matched
    miss_cost=missed*cutoff**2/2;false_cost=false*cutoff**2/2
    return dict(gospa_m=math.sqrt(localisation+miss_cost+false_cost),
                localisation_cost_m2=localisation,missed_cost_m2=miss_cost,false_cost_m2=false_cost,
                matched=matched,missed=missed,false=false)


def _read_database(path,cancel):
    file=Path(path).expanduser().resolve()
    if not file.is_file():raise FileNotFoundError(str(file))
    c=sqlite3.connect(file.as_uri()+'?mode=ro',uri=True,timeout=3)
    c.row_factory=sqlite3.Row
    c.set_progress_handler(lambda:1 if cancel() else 0,10000)
    try:
        c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required={'states','tracks','tracking_runs','plots','targets'}
        if not required.issubset(tables):raise ValueError('Tables manquantes : '+', '.join(sorted(required-tables)))
        data={table:[dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY '+('run_id' if table=='tracking_runs' else 'id'))]
              for table in required}
        c.commit()
    finally:c.close()
    return data


def _resolve_origin(state,plots,options):
    refs=jload(state.get('plot_refs_json'),[]) or []
    if not refs and state.get('plot_id') is not None:
        refs=[{'plot_id':state['plot_id'],'probability':1.}]
    weights=Counter(); total=0.; missing=0
    for ref in refs:
        if not isinstance(ref,dict):continue
        p=number(ref.get('probability',1.))
        if p is None or p<=0:continue
        total+=p
        plot=plots.get(ref.get('plot_id'))
        raw=jload(plot.get('raw_json'),{}) if plot else {}
        origin=raw.get('origin') if isinstance(raw,dict) else None
        if origin is None or not str(origin).strip():missing+=1;continue
        weights[str(origin)]+=p
    if not refs:return None,'sans plot',0.,0.,{}
    if not weights:return None,'origin absent ou lien invalide',total,0.,{}
    ordered=weights.most_common(); origin,mass=ordered[0]
    purity=mass/total if total else 0.
    if total+1e-8<options.min_association_mass or purity+1e-8<options.min_origin_purity:
        return None,'association ambiguë',total,purity,dict(weights)
    return origin,'plot',total,purity,dict(weights)


def _layout(run,size):
    layout=jload(run.get('state_layout_json'),None)
    if not isinstance(layout,list) or len(layout)!=size:return None
    aliases={'E':('E','X','X_M','EAST'),'N':('N','Y','Y_M','NORTH'),'U':('U','Z','Z_M','UP'),
             'vE':('VE','VX','VX_MPS'),'vN':('VN','VY','VY_MPS'),'vU':('VU','VZ','VZ_MPS')}
    normalized=[str(v).upper() for v in layout]
    return {name:next((normalized.index(v) for v in values if v in normalized),None) for name,values in aliases.items()}


def _qform(error,P):
    error=np.asarray(error,float);P=np.asarray(P,float)
    if not np.isfinite(P).all() or not np.allclose(P,P.T,rtol=1e-6,atol=1e-8):return None
    try:
        L=np.linalg.cholesky(P)
        y=np.linalg.solve(L,error)
        return float(y@y)
    except np.linalg.LinAlgError:return None


def _finite_values(rows,key):
    return [float(r[key]) for r in rows if number(r.get(key)) is not None]


def accuracy_summary(rows,options):
    h=_finite_values(rows,'error_h_m'); d=_finite_values(rows,'error_3d_m')
    velocity=_finite_values(rows,'velocity_error_h_mps')
    result={'samples':len(h)}
    for name,values in [('horizontal',h),('3d',d),('velocity_h',velocity)]:
        result[name+'_rmse']=float(np.sqrt(np.mean(np.square(values)))) if values else None
        result[name+'_mae']=float(np.mean(values)) if values else None
        result[name+'_p50']=float(np.percentile(values,50)) if values else None
        result[name+'_p95']=float(np.percentile(values,95)) if values else None
        result[name+'_max']=max(values) if values else None
    for axis in ('e','n','u','along','cross','speed','heading'):
        key={'speed':'speed_error_mps','heading':'heading_error_deg'}.get(axis,'error_'+axis+'_m')
        values=_finite_values(rows,key)
        if axis=='heading' and values:
            angles=np.radians(values)
            result['bias_'+axis]=float(np.degrees(np.arctan2(np.sin(angles).mean(),np.cos(angles).mean())))
        else:result['bias_'+axis]=float(np.mean(values)) if values else None
    for kind in ('nees','nis'):
        usable=[r for r in rows if number(r.get(kind)) is not None]
        result[kind+'_samples']=len(usable)
        result[kind+'_normalised_mean']=float(np.mean([r[kind]/r[kind+'_dof'] for r in usable])) if usable else None
        result[kind+'_inside_interval_pct']=100*sum(r[kind+'_low']<=r[kind]<=r[kind+'_high'] for r in usable)/len(usable) if usable else None
    nees=[r for r in rows if number(r.get('nees')) is not None]
    result['position_region_coverage_pct']=100*sum(r['nees']<=chi2.ppf(options.confidence,r['nees_dof']) for r in nees)/len(nees) if nees else None
    # Intégration temporelle seulement entre états d'une même piste et même vérité, sans grands trous.
    area=weight=0.
    groups=defaultdict(list)
    for r in rows:
        groups[r['track_db_id']].append(r)
    for group in groups.values():
        group.sort(key=lambda r:r['timestamp'])
        for a,b in zip(group,group[1:]):
            dt=b['timestamp']-a['timestamp']
            if (a.get('error_h_m') is not None and b.get('error_h_m') is not None
                    and a['target_id']==b['target_id'] and 0<dt<=options.continuity_gap_s+1e-6):
                area+=dt*(a['error_h_m']**2+b['error_h_m']**2)/2;weight+=dt
    result['horizontal_time_rmse']=math.sqrt(area/weight) if weight else None
    return result


def _interpolate_estimate(rows,t,max_gap):
    if not rows:return None
    times=[r['timestamp'] for r in rows]
    k=int(np.searchsorted(times,t))
    for i in (k,k-1):
        if 0<=i<len(rows) and abs(times[i]-t)<=1e-6:return np.array(rows[i]['estimated_position_enu'])
    if k==0 or k==len(rows):return None
    a,b=rows[k-1],rows[k];dt=b['timestamp']-a['timestamp']
    if dt<=0 or dt>max_gap+1e-6:return None
    alpha=(t-a['timestamp'])/dt
    return (1-alpha)*np.array(a['estimated_position_enu'])+alpha*np.array(b['estimated_position_enu'])


def evaluate_database(path,options=None,cancel=None):
    options=(options or MetricsOptions()).validate();cancel=cancel or (lambda:False)
    def check():
        if cancel():raise InterruptedError('Calcul annulé')
    data=_read_database(path,cancel);check()
    warnings=[];counts=Counter()
    transform=Transformer.from_crs(4979,4978,always_xy=True)
    # Une vérité par identifiant et date ; la dernière réception gagne.
    truth_samples=defaultdict(dict);names={}
    for row in data['targets']:
        check();raw=jload(row.get('raw_json'),{})
        raw=raw if isinstance(raw,dict) else {}
        t=timestamp(raw.get('timestamp'))
        if t is None:counts['truth_without_timestamp']+=1;continue
        position=unwrap(raw.get('position',{}));position=position if isinstance(position,dict) else {}
        lon=number(row.get('longitude'));lat=number(row.get('latitude'))
        if lon is None:lon=number(position.get('longitude'))
        if lat is None:lat=number(position.get('latitude'))
        altitude=number(row.get('altitude'))
        if altitude is None:altitude=number(position.get('altitude'))
        if lon is None or lat is None or not (-180<=lon<=180 and -90<=lat<=90):
            counts['truth_invalid_position']+=1;continue
        known=altitude is not None
        # Hauteur nulle de secours UNIQUEMENT pour les projections horizontales.
        ecef=np.array(transform.transform(lon,lat,altitude if known else 0.))
        identifier=str(row['target_id']);names[identifier]=str(row.get('name') or identifier)
        old=truth_samples[identifier].get(t)
        if old is not None:
            counts['truth_duplicate_timestamps']+=1
            if np.linalg.norm(old['ecef']-ecef)>.01:counts['truth_conflicting_duplicates']+=1
        truth_samples[identifier][t]={'timestamp':t,'ecef':ecef,'altitude_known':known}
    truths={identifier:TruthTrajectory(identifier,names[identifier],[values[t] for t in sorted(values)])
            for identifier,values in truth_samples.items()}
    if not truths:raise ValueError('Aucune vérité avec timestamp et position WGS84 valides dans targets.raw_json')
    t0=min(t.times[0] for t in truths.values());t1=max(t.times[-1] for t in truths.values())
    start=t0+(options.start_offset_s or 0)
    end=t1 if options.end_offset_s is None else t0+options.end_offset_s
    if start>end:raise ValueError('Début de fenêtre après la fin')
    if end< t0 or start>t1:raise ValueError('Fenêtre hors des vérités enregistrées')
    start,end=max(start,t0),min(end,t1)
    plot_map={r['id']:r for r in data['plots']}
    run_map={r['run_id']:r for r in data['tracking_runs']}
    track_map={r['id']:r for r in data['tracks']}
    by_track=defaultdict(list);before_window=defaultdict(list)
    for row in data['states']:
        if start-1e-6<=row['timestamp']<=end+1e-6:by_track[row['track_db_id']].append(row)
        else:
            counts['states_outside_window']+=1
            if row['timestamp']<start-1e-6:before_window[row['track_db_id']].append(row)
    evaluated=[];run_frames={};run_warnings=defaultdict(list)
    for run_id,run in run_map.items():
        reference=jload(run.get('reference_frame_json'),{}) or {}
        try:
            if reference.get('crs')!='local_ENU':raise ValueError('Repère non ENU')
            run_frames[run_id]=Frame(reference['origin_wgs84'])
        except (KeyError,ValueError,TypeError):run_warnings[run_id].append('Origine ENU absente/invalide : précision indisponible')
    for track_id,source_rows in by_track.items():
        check();track=track_map.get(track_id)
        if track is None:counts['orphan_states']+=len(source_rows);continue
        run=run_map.get(track['run_id'])
        if run is None:counts['orphan_runs']+=len(source_rows);continue
        source_rows.sort(key=lambda r:(r['timestamp'],r.get('step_index',0),r['id']))
        if options.last_state_per_timestamp:
            unique={r['timestamp']:r for r in source_rows}
            counts['states_same_time_removed']+=len(source_rows)-len(unique)
            source_rows=list(unique.values())
        previous_label=None; cumulative=0.;matched_count=0
        # Une fenêtre débutant par une prédiction peut utiliser l'association passée,
        # sans consulter les associations futures ni inclure ces états dans les scores.
        for old in sorted(before_window[track_id],key=lambda r:(r['timestamp'],r.get('step_index',0),r['id'])):
            label,source,*_=_resolve_origin(old,plot_map,options)
            if label is not None:previous_label=label
            elif not (source=='sans plot' and (old.get('phase')=='prediction' or old.get('coasting'))):previous_label=None
        for state in source_rows:
            check()
            row={'state_id':state['id'],'run_id':track['run_id'],'track_db_id':track_id,
                'track_id':track['track_id'],'timestamp':float(state['timestamp']),
                'elapsed_s':float(state['timestamp']-t0),'step_index':state['step_index'],
                'phase':state.get('phase'),'status':state.get('status'),'coasting':bool(state.get('coasting')),
                'plot_id':state.get('plot_id'),'target_id':None,'target_name':None,'valid':False,
                'reason':None,'estimated_position_enu':None,
                'model_probabilities':jload(state.get('model_probabilities_json'),{}) or {}}
            label,source,mass,purity,weights=_resolve_origin(state,plot_map,options)
            if source=='sans plot' and (state.get('phase')=='prediction' or state.get('coasting')) and previous_label:
                label=previous_label;source='propagée en prédiction'
            elif label is None:previous_label=None
            if label is not None:previous_label=label
            row.update(target_id=label,target_name=names.get(label,label),origin_source=source,
                       association_mass=mass,origin_purity=purity,origin_weights=weights)
            if options.confirmed_only and state.get('status') not in ('confirmée','confirmed'):
                row['reason']='piste non confirmée'
            elif not options.include_predictions and state.get('phase')=='prediction':row['reason']='prédiction exclue'
            try:x=np.asarray(jload(state.get('state_json'),[]),float)
            except (ValueError,TypeError):x=np.array([])
            try:P=np.asarray(jload(state.get('covariance_json'),[]),float)
            except (ValueError,TypeError):P=np.array([])
            mapping=_layout(run,len(x)) if x.ndim==1 else None
            frame=run_frames.get(track['run_id'])
            if mapping and mapping['E'] is not None and mapping['N'] is not None and np.isfinite(x).all():
                ids=[mapping['E'],mapping['N'],mapping['U']]
                point=np.array([x[i] if i is not None else 0. for i in ids])
                row['estimated_position_enu']=point.tolist()
                row['estimated_dimension']=3 if mapping['U'] is not None else 2
            elif row['reason'] is None:row['reason']='état ou ordre des composantes invalide'
            diag=jload(state.get('diagnostics_json'),{}) or {}
            if not isinstance(diag,dict):diag={}
            nis=number(diag.get('NIS'))
            innovation=diag.get('innovation')
            if nis is not None and isinstance(innovation,list) and len(innovation)>0:
                dof=len(innovation);low,high=chi2.interval(options.confidence,dof)
                row.update(nis=nis,nis_dof=dof,nis_low=float(low),nis_high=float(high),nis_normalised=nis/dof)
            if label is None and row['reason'] is None:row['reason']=source
            if label is not None and label not in truths and row['reason'] is None:row['reason']='origin absent de targets'
            if frame is None and row['reason'] is None:row['reason']='origine ENU indisponible'
            truth,reason=truths[label].interpolate(row['timestamp'],options.truth_max_gap_s) if label in truths else (None,None)
            if truth is None and row['reason'] is None:row['reason']=reason
            if row['reason'] is None:
                target=frame.local(truth['ecef']);point=np.array(row['estimated_position_enu'])
                e=point-target
                has_z=mapping['U'] is not None and truth['altitude_known']
                row.update(valid=True,truth_position_enu=target.tolist(),error_e_m=float(e[0]),error_n_m=float(e[1]),
                    error_u_m=float(e[2]) if has_z else None,error_h_m=float(np.linalg.norm(e[:2])),
                    error_3d_m=float(np.linalg.norm(e)) if has_z else None,truth_gap_s=truth['gap_s'])
                cumulative+=row['error_h_m']**2;matched_count+=1
                row['rmse_h_cumulative_m']=math.sqrt(cumulative/matched_count)
                velocity=truth['velocity_ecef']
                if velocity is not None and mapping['vE'] is not None and mapping['vN'] is not None:
                    vtruth=frame.velocity(velocity)
                    v=np.array([x[mapping[key]] if mapping[key] is not None else 0. for key in ('vE','vN','vU')])
                    speed=float(np.linalg.norm(vtruth[:2]));est_speed=float(np.linalg.norm(v[:2]))
                    row.update(truth_velocity_enu=vtruth.tolist(),velocity_error_h_mps=float(np.linalg.norm((v-vtruth)[:2])),
                               speed_error_mps=est_speed-speed)
                    if speed>.1:
                        direction=vtruth[:2]/speed
                        row['error_along_m']=float(e[:2]@direction)
                        row['error_cross_m']=float(e[0]*direction[1]-e[1]*direction[0])
                        if est_speed>.1:
                            row['heading_error_deg']=float((np.degrees(np.arctan2(v[0],v[1])-np.arctan2(vtruth[0],vtruth[1]))+180)%360-180)
                dim=options.nees_dimension
                if dim==2 or has_z:
                    ids=[mapping['E'],mapping['N']]+([mapping['U']] if dim==3 else [])
                    if P.shape==(len(x),len(x)):
                        value=_qform(e[:dim],P[np.ix_(ids,ids)])
                        if value is not None:
                            low,high=chi2.interval(options.confidence,dim)
                            row.update(nees=value,nees_dof=dim,nees_low=float(low),nees_high=float(high),nees_normalised=value/dim)
                        else:counts['invalid_position_covariance']+=1
            evaluated.append(row)
    rows_by_track=defaultdict(list);rows_by_run=defaultdict(list)
    for row in evaluated:
        rows_by_track[row['track_db_id']].append(row)
        rows_by_run[row['run_id']].append(row)
    # Synthèses par piste.
    track_summaries=[]
    for track_id,track in track_map.items():
        check();rows=rows_by_track[track_id]
        if not rows:continue
        rows.sort(key=lambda r:r['timestamp'])
        valid=[r for r in rows if r['valid']]
        labels=[r['target_id'] for r in rows if r['target_id'] is not None]
        origin_counts=Counter(labels);dominant=origin_counts.most_common(1)
        switches=sum(a!=b for a,b in zip(labels,labels[1:]))
        confirmed=next((r['timestamp'] for r in rows if r['status'] in ('confirmée','confirmed')),None)
        summary=dict(track_db_id=track_id,run_id=track['run_id'],track_id=track['track_id'],states=len(rows),
            evaluated_states=len(valid),observed_duration_s=rows[-1]['timestamp']-rows[0]['timestamp'],
            dominant_target_id=dominant[0][0] if dominant else None,
            dominant_target_name=names.get(dominant[0][0]) if dominant else None,
            association_purity_pct=100*dominant[0][1]/len(labels) if dominant else None,
            origin_switches=switches,coasting_samples_pct=100*sum(r['coasting'] for r in rows)/len(rows),
            first_confirmation_observed_delay_s=confirmed-track['birth_timestamp'] if confirmed is not None else None,
            first_timestamp=rows[0]['timestamp'],last_timestamp=rows[-1]['timestamp'])
        summary.update(accuracy_summary(rows,options));track_summaries.append(summary)
    # Segments valides par vérité et piste ; une association différente coupe le segment.
    target_summaries=[];all_segments=[]
    for run_id in run_map:
        for target_id,truth in truths.items():
            check();support=truth.support(options.truth_max_gap_s,start,end);total=duration(support)
            target_segments=[];target_rows=[]
            for track_id in track_map:
                if track_map[track_id]['run_id']!=run_id:continue
                rows=rows_by_track[track_id]
                current=[]
                def finish():
                    if current:
                        for a,b in intersect_intervals(segments(current,options.continuity_gap_s),support):
                            target_segments.append((a,b,track_id))
                        current.clear()
                for row in rows:
                    if row['valid'] and row['target_id']==target_id:
                        current.append(row['timestamp']);target_rows.append(row)
                    else:finish()
                finish()
            merged=merge_intervals([(a,b) for a,b,_ in target_segments]);covered=duration(merged)
            corrected=intersect_intervals(segments([r['timestamp'] for r in target_rows if r['phase']!='prediction'],options.continuity_gap_s),support)
            gaps=[]
            for a,b in support:
                cursor=a
                for x,y in merged:
                    if y<a or x>b:continue
                    if x>cursor:gaps.append(x-cursor)
                    cursor=max(cursor,y)
                if cursor<b:gaps.append(b-cursor)
            winners={}
            for row in target_rows:
                old=winners.get(row['timestamp'])
                if old is None or row['error_h_m']<old['error_h_m']:winners[row['timestamp']]=row
            ordered=[winners[t] for t in sorted(winners)]
            switches=sum(a['track_db_id']!=b['track_db_id'] and b['timestamp']-a['timestamp']<=options.continuity_gap_s+1e-6
                         for a,b in zip(ordered,ordered[1:]))
            first=min((r['timestamp'] for r in target_rows),default=None)
            confirmed=min((r['timestamp'] for r in target_rows if r['status'] in ('confirmée','confirmed')),default=None)
            summary=dict(run_id=run_id,target_id=target_id,target_name=truth.name,
                truth_samples=len(truth.times),truth_available_duration_s=total,tracked_duration_s=covered,
                coverage_pct=100*covered/total if total>0 else None,
                corrected_coverage_pct=100*duration(corrected)/total if total>0 else None,
                segments=len(merged),fragmentations=max(0,len(merged)-1),
                longest_segment_s=max((b-a for a,b in merged),default=0.),
                longest_gap_s=max(gaps,default=0.) if total>0 else None,
                first_track_delay_s=first-support[0][0] if first is not None and support else None,
                first_confirmed_delay_s=confirmed-support[0][0] if confirmed is not None and support else None,
                track_ids=sorted({track_map[id]['track_id'] for _,_,id in target_segments}),
                track_db_ids=sorted({id for _,_,id in target_segments}),identity_switches=switches)
            summary.update(accuracy_summary(target_rows,options));target_summaries.append(summary)
            for a,b,track_id in target_segments:
                all_segments.append(dict(run_id=run_id,target_id=target_id,target_name=truth.name,
                    track_db_id=track_id,track_id=track_map[track_id]['track_id'],start=a,end=b))
    # Courbes de cardinalité et GOSPA facultatif, sans extrapolation des pistes.
    if (end-start)/options.grid_step_s>20000:raise ValueError('Plus de 20 000 points de grille : augmenter le pas temporel')
    grid=np.arange(start,end+1e-6,options.grid_step_s)
    if not len(grid) or end-grid[-1]>1e-6:grid=np.r_[grid,end]
    scene=[]
    for run_id in run_map:
        frame=run_frames.get(run_id)
        if frame is None:continue
        series={key:[r for r in rows_by_track[key] if r['estimated_position_enu'] is not None
                            and r.get('estimated_dimension',2)>=options.nees_dimension
                            and (not options.confirmed_only or r['status'] in ('confirmée','confirmed'))
                            and (options.include_predictions or r['phase']!='prediction')]
                for key in track_map if track_map[key]['run_id']==run_id}
        # Même avec tous les états affichés, la scène contient une position par piste/date.
        series={key:list({r['timestamp']:r for r in rows}.values()) for key,rows in series.items()}
        for t in grid:
            check();true_points=[]
            for truth in truths.values():
                point,_=truth.interpolate(t,options.truth_max_gap_s)
                if point is not None and (options.nees_dimension==2 or point['altitude_known']):true_points.append(frame.local(point['ecef'])[:options.nees_dimension])
            estimates=[]
            for rows in series.values():
                point=_interpolate_estimate(rows,t,options.continuity_gap_s)
                if point is not None:estimates.append(point[:options.nees_dimension])
            row=dict(run_id=run_id,timestamp=float(t),elapsed_s=float(t-t0),truth_count=len(true_points),
                     track_count=len(estimates),cardinality_error=len(estimates)-len(true_points))
            if options.compute_gospa:row.update(gospa(true_points,estimates,options.gospa_cutoff_m))
            scene.append(row)
    run_summaries=[]
    for run_id,run in run_map.items():
        rows=rows_by_run[run_id]
        targets=[t for t in target_summaries if t['run_id']==run_id]
        associated=[t for t in targets if t['samples']]
        coverages=[t['coverage_pct'] for t in associated if t['coverage_pct'] is not None]
        summary={'run_id':run_id,'name':run['name'],'estimator':run['estimator'],
                 'configuration':jload(run.get('configuration_json'),{}),
                 'states':len(rows),'tracks':len({r['track_db_id'] for r in rows}),
                 'matched_targets':len(associated),'registered_targets':len(targets),
                 'associated_coverage_pct':float(np.mean(coverages)) if coverages else None,
                 'origin_switches':sum(t['origin_switches'] for t in track_summaries if t['run_id']==run_id),
                 'fragmentations':sum(t['fragmentations'] for t in targets),
                 'unlabelled_states':sum(r['target_id'] is None for r in rows),
                 'invalid_reasons':dict(Counter(r['reason'] for r in rows if r['reason'])),
                 'warnings':run_warnings[run_id]}
        summary.update(accuracy_summary(rows,options));run_summaries.append(summary)
    if counts['truth_duplicate_timestamps']:warnings.append(f"{counts['truth_duplicate_timestamps']} vérités répétées à la même date : dernière réception retenue.")
    if counts['truth_conflicting_duplicates']:warnings.append(f"{counts['truth_conflicting_duplicates']} doublons de vérité ont des positions différentes de plus de 1 cm.")
    if counts['states_same_time_removed']:warnings.append(f"{counts['states_same_time_removed']} états intermédiaires à date identique retirés pour équilibrer les capteurs.")
    if counts['states_outside_window']:warnings.append(f"{counts['states_outside_window']} états hors de la fenêtre de vérité ; vérifier également l’origine et l’unité des horloges.")
    warnings += [
        'La couverture porte sur les vérités enregistrées, sans masque de visibilité capteur ; ce n’est pas un Pd.',
        'Les retards commencent à la première vérité disponible dans la fenêtre, pas à une activation non enregistrée du filtre.',
        'NEES/NIS : bornes nominales ponctuelles ; les échantillons sont corrélés et le NIS enregistré est sélectionné après gating.',
        'L’altitude absolue altitude est utilisée ; altitude_m n’est pas confondu avec une altitude absolue. Vérifier le datum vertical.',
        'La vitesse de référence est la dérivée des positions interpolées, non une interpolation du cap/vitesse déclarés.',
        'Les résultats d’association dépendent de origin : ils ne constituent pas un appariement indépendant du tracker.',
    ]
    if options.compute_gospa:warnings.append('GOSPA compare toutes les vérités et pistes présentes ; un essai monocible dans une scène multicible aura des cibles manquées par définition.')
    report={'database':Path(path).name,'options':asdict(options),'time_origin':float(t0),
        'window_start':float(start),'window_end':float(end),
        'counts':dict(counts,raw_states=len(data['states']),retained_states=len(evaluated),
                      raw_truth_rows=len(data['targets']),unique_truth_rows=sum(len(t.times) for t in truths.values()),
                      plots=len(data['plots'])),
        'runs':run_summaries,'states':evaluated,'tracks':track_summaries,'targets':target_summaries,
        'segments':all_segments,'scene':scene,'warnings':warnings}
    return clean_json(report)


def export_report(report,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    paths=[]
    p=directory/'performances.json'
    p.write_text(json.dumps(clean_json(report),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8');paths.append(p)
    for section in ('states','tracks','targets','scene'):
        rows=report[section];p=directory/(section+'.csv')
        fields=list(dict.fromkeys(key for row in rows for key in row))
        with p.open('w',newline='',encoding='utf-8-sig') as output:
            writer=csv.DictWriter(output,fieldnames=fields,delimiter=';');writer.writeheader()
            for row in rows:
                writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})
        paths.append(p)
    return paths
