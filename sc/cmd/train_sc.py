#!/usr/bin/env python

import argparse

import torch
from sc.clustering.trainer import Trainer
import os
import yaml
import datetime
import socket
import ipyparallel as ipp
import sys

engine_id = -1

def get_parallel_map_func(work_dir="."):
    c = ipp.Client(url_file=f"{work_dir}/ipypar/security/ipcontroller-client.json")
    print("Engine IDs:", c.ids)
    with c[:].sync_imports():
        from sc.clustering.trainer import Trainer
        import os
        import yaml
        import datetime
        import socket
        import torch
        import sys
    c[:].push(dict(run_training=run_training),
              block=True)
    for i in c.ids:
        c[i].push({"engine_id": i}, block=True)

    return c[:].map_sync, len(c.ids)

def run_training(job_number, work_dir, trainer_config, max_epoch, verbose, data_file):
    work_dir = f'{work_dir}/training/job_{job_number+1}'
    if not os.path.exists(work_dir):
        os.makedirs(work_dir, exist_ok=True)
    original_stdout = sys.stdout 
    original_stderr = sys.stderr 
    with open(f'{work_dir}/messages.txt', 'w') as f:
        sys.stdout = f
        sys.stderr = f 
        ngpus_per_node = torch.cuda.device_count()
        if "SLURM_LOCALID" in os.environ:
            local_id = int(os.environ.get("SLURM_LOCALID", 0))
        elif socket.gethostname() == 'WNC-167339':
            local_id = engine_id
            assert not (job_number==0 and local_id == -1)
            if local_id == -1:
                local_id = 0
        igpu = local_id % ngpus_per_node if torch.cuda.is_available() else -1

        trainer = Trainer.from_data(data_file,
                                    igpu=igpu,
                                    max_epoch=max_epoch,
                                    verbose=verbose,
                                    work_dir=work_dir,
                                    **trainer_config)
        t1 = datetime.datetime.now()
        print(f"Training started at {t1} on {socket.gethostname()}")
        metrics = trainer.train()
        t2 = datetime.datetime.now()
        print('training finished at', t2)
        print(f"Total {(t2 - t1).seconds + (t2 - t1).microseconds * 1.0E-6 :.2f}s used in traing")
        print(metrics)
        n_coord_num = trainer_config.get("n_coord_num", 3)
        trainer.test_models(data_file, n_coord_num=n_coord_num, work_dir=work_dir)
        sys.stdout.flush()
        sys.stderr.flush()
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    return metrics

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--data_file', type=str, required=True,
                        help='File name of the dataset in CSV format')
    parser.add_argument('-c', '--config', type=str, required=True,
                        help='Config for training parameter in YAML format')
    parser.add_argument('-e', '--max_epoch', type=int, default=2000,
                        help='Maximum iterations')
    parser.add_argument('-v', '--verbose', action="store_true",
                        help='Maximum iterations')
    parser.add_argument('-w', "--work_dir", type=str, default='.',
                        help="Working directory to write the output files")
    parser.add_argument('--trials', type=int, default=1,
                        help='Total number of trainings to run')                    
    args = parser.parse_args()

    work_dir = os.path.expandvars(os.path.expanduser(args.work_dir))
    work_dir = os.path.abspath(work_dir)
    with open(os.path.expandvars(os.path.expanduser(args.config))) as f:
        trainer_config = yaml.full_load(f)

    if not os.path.exists(work_dir):
        os.makedirs(work_dir, exist_ok=True)
    
    max_epoch = args.max_epoch
    verbose = args.verbose
    data_file = os.path.abspath(os.path.expandvars(os.path.expanduser(args.data_file)))
    trails = args.trials

    if trails > 1:
        par_map, nprocesses = get_parallel_map_func(work_dir)
    else:
        par_map, nprocesses = map, 1
    print("running with {} processes".format(nprocesses))

    result = par_map(run_training,
                     list(range(trails)), 
                     [work_dir]*trails, 
                     [trainer_config]*trails, 
                     [max_epoch]*trails, 
                     [verbose]*trails, 
                     [data_file]*trails)
    list(result)

if __name__ == '__main__':
    main()
