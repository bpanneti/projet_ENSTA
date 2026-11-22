"""Dock de performances indépendant du moteur de tracking et du recorder.

Lancement autonome : python performance_widget.py chemin/recording.sqlite
"""
from collections import defaultdict
from pathlib import Path
import json
import sys
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,
    QLabel,QLineEdit,QPushButton,QFileDialog,QComboBox,QDoubleSpinBox,QCheckBox,
    QTabWidget,QScrollArea,QGroupBox,QTreeWidget,QTreeWidgetItem,QPlainTextEdit,
    QMessageBox,QHeaderView)
from tracking_metrics import MetricsOptions,evaluate_database,export_report,accuracy_summary


COLORS=['#52d6cb','#ffbd69','#9e9cff','#ff7d96','#a4dc80','#72b9ff']


def fmt(value,unit=''):
    return '—' if value is None else f'{value:.2f}{unit}' if isinstance(value,(float,int)) else str(value)


class MetricsWorker(QThread):
    result_ready=pyqtSignal(object)
    failed=pyqtSignal(str)

    def __init__(self,path,options,parent=None):
        super().__init__(parent);self.path=path;self.options=options

    def run(self):
        try:
            report=evaluate_database(self.path,self.options,self.isInterruptionRequested)
            if not self.isInterruptionRequested():self.result_ready.emit(report)
        except InterruptedError:pass
        except Exception as exc:
            if not self.isInterruptionRequested():self.failed.emit(f'{type(exc).__name__} : {exc}')


class TrackingPerformanceWidget(QWidget):
    def __init__(self,parent=None,database_path_provider=None):
        super().__init__(parent);self.provider=database_path_provider
        self.setObjectName('root')
        self.setStyleSheet('QTabWidget::pane{background:#0c1522;border:1px solid #31465e;} QTabBar::tab{background:#17283b;color:#a9bed4;padding:8px;} QTabBar::tab:selected{background:#294b69;color:#ffffff;} QScrollArea{background:#0c1522;border:0;} QScrollArea>QWidget{background:#0c1522;}')
        self.worker=None;self.report=None;self._loading=False
        root=QVBoxLayout(self);root.setContentsMargins(10,10,10,10)
        title=QLabel('MESURES DE PERFORMANCE');title.setStyleSheet('font-size:18px;font-weight:700;color:#52d6cb')
        root.addWidget(title)
        self.path=QLineEdit();self.path.setPlaceholderText('Choisir une base SQLite de tracking…')
        browse=QPushButton('Ouvrir…');browse.clicked.connect(self.choose_database)
        current=QPushButton('Base courante');current.setEnabled(self.provider is not None);current.clicked.connect(self.use_current)
        self.calculate_button=QPushButton('Calculer / actualiser');self.calculate_button.clicked.connect(self.calculate)
        line=QHBoxLayout();line.addWidget(self.path,1);line.addWidget(browse);line.addWidget(current);root.addLayout(line)
        line=QHBoxLayout();line.addWidget(self.calculate_button)
        self.export_button=QPushButton('Exporter CSV + JSON');self.export_button.setEnabled(False);self.export_button.clicked.connect(self.export)
        line.addWidget(self.export_button)
        self.status=QLabel('Lecture seule · calcul sur les dates de simulation enregistrées');line.addWidget(self.status,1);root.addLayout(line)
        settings=QGroupBox('Paramètres d’évaluation');grid=QGridLayout(settings)
        def spin(label,value,lo,hi,row,col,decimals=2):
            control=QDoubleSpinBox();control.setRange(lo,hi);control.setDecimals(decimals);control.setValue(value)
            grid.addWidget(QLabel(label),row,col);grid.addWidget(control,row,col+1);return control
        self.truth_gap=spin('Trou vérité maximal (s)',1,.001,3600,0,0,3)
        self.track_gap=spin('Trou piste maximal (s)',1.5,.001,3600,0,2,3)
        self.grid_step=spin('Pas scène (s)',.5,.01,3600,1,0)
        self.cutoff=spin('Seuil GOSPA (m)',100,1,100000,1,2)
        self.purity=spin('Pureté origin minimale',.6,.501,1,2,0,3)
        self.mass=spin('Masse associée minimale',.5,0,1,2,2,3)
        self.dimension=QComboBox();self.dimension.addItems(['2D horizontal','3D'])
        grid.addWidget(QLabel('NEES / GOSPA'),3,0);grid.addWidget(self.dimension,3,1)
        self.confidence=spin('Confiance nominale',.95,.501,.9999,3,2,4)
        self.confirmed=QCheckBox('Pistes confirmées uniquement')
        self.predictions=QCheckBox('Inclure les prédictions');self.predictions.setChecked(True)
        self.last_only=QCheckBox('Dernier état par piste et date');self.last_only.setChecked(True)
        self.gospa=QCheckBox('Calculer GOSPA (scène complète)')
        for i,c in enumerate((self.confirmed,self.predictions,self.last_only,self.gospa)):grid.addWidget(c,4+i//2,(i%2)*2,1,2)
        self.window_enabled=QCheckBox('Limiter la fenêtre (secondes depuis la première vérité)');grid.addWidget(self.window_enabled,6,0,1,4)
        self.start=spin('Début (s)',0,0,1e9,7,0);self.end=spin('Fin (s)',60,0,1e9,7,2)
        settings.setVisible(False)
        toggle=QPushButton('Réglages ▾');toggle.setCheckable(True);toggle.toggled.connect(settings.setVisible)
        root.addWidget(toggle);root.addWidget(settings)
        filters=QHBoxLayout();self.run=QComboBox();self.track=QComboBox();self.target=QComboBox()
        for label,c in [('Essai',self.run),('Piste',self.track),('Vérité',self.target)]:filters.addWidget(QLabel(label));filters.addWidget(c,1)
        root.addLayout(filters)
        self.run.currentIndexChanged.connect(self.run_changed)
        self.track.currentIndexChanged.connect(self.render);self.target.currentIndexChanged.connect(self.render)
        self.summary=QLabel('Ouvrir un enregistrement puis calculer.');self.summary.setWordWrap(True)
        self.summary.setStyleSheet('color:#dce9f7;padding:12px;background:#192b3d;border-radius:8px;font-size:13px')
        root.addWidget(self.summary)
        self.tabs=QTabWidget();root.addWidget(self.tabs,1)
        self.plots={};self._pages={}
        specifications=[('Précision',[
            ('errors','Erreur de position','Erreur (m)'),('rmse','RMSE horizontale cumulée','RMSE (m)'),
            ('axes','Erreurs signées Est / Nord / Haut','Erreur (m)'),('ecdf','Distribution des erreurs horizontales','Probabilité cumulée')]),
            ('Cinématique', [('velocity','Erreur de vitesse horizontale','Erreur (m/s)'),
             ('heading','Erreur de cap','Erreur (°)'),('along','Erreurs longitudinale / transversale','Erreur (m)')]),
            ('Continuité',[('continuity','Segments de suivi valides','Cible / piste'),
             ('cardinality','Cardinalités de la scène complète','Nombre')]),
            ('Cohérence', [('nees','NEES / dimension','Valeur normalisée'),('nis','NIS / dimension','Valeur normalisée')]),
            ('Associations / IMM',[('purity','Pureté et masse des associations','Probabilité'),
             ('models','Probabilités des modèles','Probabilité')]),
            ('GOSPA',[('gospa','GOSPA · scène complète','Distance (m)'),
             ('costs','Décomposition GOSPA (coûts au carré)','Coût (m²)')])]
        for name,specs in specifications:
            page=QWidget();page.setObjectName('root');layout=QVBoxLayout(page)
            for key,title,ylabel in specs:
                plot=pg.PlotWidget(background='#101d2c');plot.setMinimumHeight(240)
                plot.setTitle(title,color='#d2e5f4');plot.setLabel('left',ylabel)
                plot.setLabel('bottom','Temps depuis la première vérité (s)' if key!='ecdf' else 'Erreur horizontale (m)')
                plot.showGrid(x=True,y=True,alpha=.2);plot.addLegend(offset=(8,8),labelTextColor='#dce9f7',brush=pg.mkBrush(16,29,44,210))
                self.plots[key]=plot;layout.addWidget(plot)
            if name=='Cohérence':
                note=QLabel('Bornes χ² ponctuelles nominales. NIS sélectionné après gating ; les états successifs sont corrélés.\nLa NEES porte sur la position, selon la dimension choisie.');note.setWordWrap(True);layout.addWidget(note)
            scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(page);self.tabs.addTab(scroll,name)
        table_page=QWidget();table_page.setObjectName('root');layout=QVBoxLayout(table_page);self.tables={}
        for name in ('Pistes','Vérités terrain'):
            layout.addWidget(QLabel(name));table=QTreeWidget();table.setRootIsDecorated(False);table.setAlternatingRowColors(True)
            table.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            self.tables[name]=table;layout.addWidget(table)
        self.tabs.addTab(table_page,'Tableaux')
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.tabs.addTab(self.details,'Qualité / méthode')

    def choose_database(self):
        path,_=QFileDialog.getOpenFileName(self,'Enregistrement SQLite','','SQLite (*.sqlite *.sqlite3 *.db);;Tous (*)')
        if path:self.path.setText(path);self.calculate()

    def use_current(self):
        try:
            path=self.provider()
            if path is None:raise ValueError('Aucune base courante')
            self.path.setText(str(path));self.calculate()
        except Exception as exc:self.status.setText(str(exc))

    def options(self):
        return MetricsOptions(truth_max_gap_s=self.truth_gap.value(),continuity_gap_s=self.track_gap.value(),
            grid_step_s=self.grid_step.value(),confidence=self.confidence.value(),nees_dimension=2+self.dimension.currentIndex(),
            confirmed_only=self.confirmed.isChecked(),include_predictions=self.predictions.isChecked(),
            last_state_per_timestamp=self.last_only.isChecked(),min_origin_purity=self.purity.value(),
            min_association_mass=self.mass.value(),compute_gospa=self.gospa.isChecked(),gospa_cutoff_m=self.cutoff.value(),
            start_offset_s=self.start.value() if self.window_enabled.isChecked() else None,
            end_offset_s=self.end.value() if self.window_enabled.isChecked() else None)

    def calculate(self):
        if self.worker is not None and self.worker.isRunning():return
        try:
            options=self.options().validate()
            if not self.path.text().strip():raise ValueError('Choisir une base SQLite')
        except ValueError as exc:self.status.setText(str(exc));return
        self.calculate_button.setEnabled(False);self.status.setText('Calcul en cours…')
        if self.worker is not None:self.worker.deleteLater()
        self.worker=MetricsWorker(self.path.text().strip(),options,self)
        self.worker.result_ready.connect(self.accept_report);self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.calculation_finished);self.worker.start()

    def calculation_finished(self):
        self.calculate_button.setEnabled(True)

    def failed(self,message):
        self.status.setText('Échec : '+message)
        self.details.setPlainText(message)

    def accept_report(self,report):
        self.report=report;self.export_button.setEnabled(True);self._loading=True
        self.run.clear()
        for run in report['runs']:
            dynamics=run['configuration'].get('dynamics','?')
            self.run.addItem(f"{run['name']} · {run['estimator']}/{dynamics} · {run['run_id'][:8]}",run['run_id'])
        self._loading=False;self.run_changed()
        self.status.setText(f"{report['counts']['retained_states']} états retenus · {report['database']}")
        diagnostic={run['run_id']:{'avertissements':run['warnings'],'etats_non_evaluables':run['invalid_reasons']} for run in report['runs']}
        self.details.setPlainText('\n\n'.join(report['warnings'])+'\n\nCompteurs :\n'+json.dumps(report['counts'],ensure_ascii=False,indent=2)+'\n\nPar essai :\n'+json.dumps(diagnostic,ensure_ascii=False,indent=2))

    def run_changed(self,*_):
        if self._loading or not self.report:return
        run_id=self.run.currentData();self._loading=True
        self.track.clear();self.track.addItem('Toutes',None)
        for row in self.report['tracks']:
            if row['run_id']==run_id:self.track.addItem(row['track_id'],row['track_db_id'])
        self.target.clear();self.target.addItem('Toutes',None)
        for row in self.report['targets']:
            if row['run_id']==run_id:self.target.addItem(f"{row['target_name']} · {row['target_id'][:8]}",row['target_id'])
        self._loading=False;self.render()

    def curve(self,key,x,y,name,color,style=Qt.PenStyle.SolidLine,width=2,symbol=None):
        yy=np.array([float(v) if v is not None else np.nan for v in y])
        if not len(yy) or not np.isfinite(yy).any():return
        self.plots[key].plot(x,yy,name=name,pen=pg.mkPen(color,width=width,style=style),connect='finite',symbol=symbol,symbolSize=5,symbolBrush=color,symbolPen=color)

    def render(self,*_):
        if self._loading or not self.report:return
        r=self.report;run_id=self.run.currentData();track_id=self.track.currentData();target_id=self.target.currentData()
        rows=[s for s in r['states'] if s['run_id']==run_id and (track_id is None or s['track_db_id']==track_id) and (target_id is None or s['target_id']==target_id)]
        for p in self.plots.values():p.clear();p.getPlotItem().legend.clear()
        options=MetricsOptions(**r['options']);stats=accuracy_summary(rows,options)
        run=next((x for x in r['runs'] if x['run_id']==run_id),{})
        self.summary.setText(f"RMSE horizontale : {fmt(stats['horizontal_rmse'],' m')}   ·   P95 : {fmt(stats['horizontal_p95'],' m')}   ·   RMSE 3D : {fmt(stats['3d_rmse'],' m')}\n"
            f"Biais vertical : {fmt(stats['bias_u'],' m')}   ·   {stats['samples']} états évalués / {len(rows)} affichés\n"
            f"Essai complet : couverture des vérités associées {fmt(run.get('associated_coverage_pct'),' %')} · {run.get('matched_targets',0)} / {run.get('registered_targets',0)} cibles associées · {run.get('origin_switches',0)} changements d’origin")
        groups=defaultdict(list)
        for row in rows:groups[row['track_db_id']].append(row)
        for idx,group in enumerate(groups.values()):
            group.sort(key=lambda s:s['timestamp']);color=COLORS[idx%len(COLORS)];name=group[0]['track_id'];t=[s['elapsed_s'] for s in group]
            # Couper les lignes aux trous, changements d'origin et états non évaluables.
            def series(key):
                xx=[];yy=[];previous=None;total=0.;count=0
                for s in group:
                    if previous is not None and (s['timestamp']-previous['timestamp']>options.continuity_gap_s or s['target_id']!=previous['target_id']):xx.append(np.nan);yy.append(None)
                    value=s.get(key)
                    if key=='rmse_h_cumulative_m':
                        if s.get('error_h_m') is not None:
                            total+=s['error_h_m']**2;count+=1;value=float(np.sqrt(total/count))
                        else:value=None
                    xx.append(s['elapsed_s']);yy.append(value);previous=s
                return xx,yy
            for key,field,label,style in [('errors','error_h_m','horizontal',Qt.PenStyle.SolidLine),('errors','error_3d_m','3D',Qt.PenStyle.DashLine),('rmse','rmse_h_cumulative_m','RMSE',Qt.PenStyle.SolidLine),('velocity','velocity_error_h_mps','vitesse',Qt.PenStyle.SolidLine),('heading','heading_error_deg','cap',Qt.PenStyle.SolidLine)]:
                xx,yy=series(field);self.curve(key,xx,yy,f'{name} · {label}',color,style,symbol='o')
            for key,fields in [('axes',[('error_e_m','Est'),('error_n_m','Nord'),('error_u_m','Haut')]),('along',[('error_along_m','Longitudinal'),('error_cross_m','Transversal')])]:
                for j,(field,label) in enumerate(fields):
                    xx,yy=series(field);self.curve(key,xx,yy,f'{name} · {label}',COLORS[(idx+j)%len(COLORS)])
            errors=sorted(s['error_h_m'] for s in group if s.get('error_h_m') is not None)
            self.curve('ecdf',errors,np.arange(1,len(errors)+1)/len(errors) if errors else [],name,color)
            for kind in ('nees','nis'):
                self.curve(kind,t,[s.get(kind+'_normalised') for s in group],name,color,symbol='o')
                for bound in ('low','high'):
                    self.curve(kind,t,[s[kind+'_'+bound]/s[kind+'_dof'] if s.get(kind+'_dof') else None for s in group],f'{name} χ² {bound}',color,Qt.PenStyle.DotLine,1)
            for field,style in [('origin_purity',Qt.PenStyle.SolidLine),('association_mass',Qt.PenStyle.DashLine)]:self.curve('purity',t,[s.get(field) for s in group],name+' '+field,color,style)
            models=[s['model_probabilities'] for s in group]
            indices=set()
            for m in models:indices.update(range(len(m)) if isinstance(m,list) else m.keys() if isinstance(m,dict) else [])
            for j,k in enumerate(sorted(indices,key=str)):
                values=[m[k] if isinstance(m,list) and isinstance(k,int) and k<len(m) else m.get(k) if isinstance(m,dict) else None for m in models]
                self.curve('models',t,values,f'{name} · modèle {k}',COLORS[(idx+j)%len(COLORS)])
        selected_targets=[x for x in r['targets'] if x['run_id']==run_id and (target_id is None or x['target_id']==target_id)]
        lanes=[]
        for seg in r['segments']:
            if seg['run_id']!=run_id or (target_id and seg['target_id']!=target_id) or (track_id is not None and seg['track_db_id']!=track_id):continue
            lane=(seg['target_name'],seg['track_id'])
            if lane not in lanes:lanes.append(lane)
            y=lanes.index(lane)+1
            self.curve('continuity',[seg['start']-r['time_origin'],seg['end']-r['time_origin']],[y,y],' / '.join(lane),COLORS[(y-1)%len(COLORS)],width=8,symbol='o')
        self.plots['continuity'].getAxis('left').setTicks([[(i+1,' / '.join(l)) for i,l in enumerate(lanes)]])
        scene=[s for s in r['scene'] if s['run_id']==run_id];t=[s['elapsed_s'] for s in scene]
        for j,(field,label) in enumerate([('truth_count','Vérités'),('track_count','Pistes'),('cardinality_error','Pistes − vérités')]):self.curve('cardinality',t,[s[field] for s in scene],label,COLORS[j])
        for key,fields in [('gospa',[('gospa_m','GOSPA')]),('costs',[('localisation_cost_m2','Localisation'),('missed_cost_m2','Manquées'),('false_cost_m2','Fausses')])]:
            for j,(field,label) in enumerate(fields):self.curve(key,t,[s.get(field) for s in scene],label,COLORS[j])
        self.fill_table('Pistes',[x for x in r['tracks'] if x['run_id']==run_id and (track_id is None or x['track_db_id']==track_id)],
            [('track_id','Piste'),('dominant_target_name','Vérité dominante'),('samples','États évalués'),('horizontal_rmse','RMSE H (m)'),('horizontal_p95','P95 H (m)'),('3d_rmse','RMSE 3D (m)'),('bias_u','Biais U (m)'),('association_purity_pct','Pureté (%)'),('origin_switches','Changements origin'),('first_confirmation_observed_delay_s','Confirmation depuis naissance (s)'),('coasting_samples_pct','Coasting (%)'),('nees_normalised_mean','NEES/d moyenne'),('nis_normalised_mean','NIS/d moyenne'),('position_region_coverage_pct','Dans région confiance (%)')])
        self.fill_table('Vérités terrain',selected_targets,[('target_name','Vérité'),('coverage_pct','Couverture (%)'),('corrected_coverage_pct','Avec correction (%)'),('tracked_duration_s','Durée suivie (s)'),('truth_available_duration_s','Vérité disponible (s)'),('fragmentations','Fragmentations'),('longest_gap_s','Trou maximal (s)'),('first_track_delay_s','Premier suivi (s)'),('first_confirmed_delay_s','Première confirmation (s)'),('identity_switches','Changements ID'),('track_ids','Pistes'),('horizontal_rmse','RMSE H (m)')])

    def fill_table(self,name,rows,columns):
        table=self.tables[name];table.clear();table.setHeaderLabels([label for _,label in columns])
        for row in rows:
            item=QTreeWidgetItem([fmt(row.get(key)) for key,_ in columns]);table.addTopLevelItem(item)
            for i,(key,_) in enumerate(columns):item.setToolTip(i,f'{key} : {row.get(key)}')

    def export(self):
        if not self.report:return
        path=QFileDialog.getExistingDirectory(self,'Exporter tous les résultats calculés')
        if path:
            try:
                files=export_report(self.report,path);self.status.setText(f'{len(files)} fichiers exportés dans {path} (tous les essais)')
            except OSError as exc:QMessageBox.warning(self,'Export des performances',str(exc))

    def shutdown(self,timeout_ms=6000):
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            return self.worker.wait(timeout_ms)
        return True

    def closeEvent(self,event):
        if self.shutdown():event.accept()
        else:event.ignore()


class PerformanceDock(QObject):
    def __init__(self,window,database_path_provider=None):
        super().__init__(window)
        self.widget=TrackingPerformanceWidget(database_path_provider=database_path_provider)
        self.dock=window._dock('Mesures de performance','performanceDock',self.widget,Qt.DockWidgetArea.RightDockWidgetArea)

    def close(self):
        return self.widget.shutdown()


if __name__=='__main__':
    app=QApplication(sys.argv)
    from theme import apply_theme
    apply_theme(app)
    window=TrackingPerformanceWidget();window.setWindowTitle('Capteurs Live · Mesures de performance');window.resize(1200,900)
    if len(sys.argv)>1:window.path.setText(str(Path(sys.argv[1]).resolve()));window.calculate()
    window.show();sys.exit(app.exec())
