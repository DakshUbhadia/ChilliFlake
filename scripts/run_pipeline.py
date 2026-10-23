import argparse, subprocess, sys

def main():
    p=argparse.ArgumentParser(); p.add_argument('--db'); p.add_argument('--runs',type=int,default=20); p.add_argument('--branch',default='dev'); a=p.parse_args()
    common=[]
    if a.db: common=['--db',a.db]
    steps=[[sys.executable,'-m','src.ingestion.run_ingestion','--runs',str(a.runs),'--branch',a.branch,*common],
           [sys.executable,'-m','src.analyzer.run_analysis','--branch',a.branch,*common],
           [sys.executable,'-m','src.analyzer.quarantine',*common,'sync'],
           [sys.executable,'-m','src.analyzer.quarantine',*common,'export','--out','quarantine.json']]
    for step in steps: subprocess.run(step,check=True)
if __name__=='__main__': main()
