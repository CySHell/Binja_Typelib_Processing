from binaryninja import *
from . import pre_process


def run(bv: BinaryView):
    log.log_to_file(0, 'pre_proc_log.txt')
    pre_process.pp(bv)


PluginCommand.register('preproc', 'preproc', run)
