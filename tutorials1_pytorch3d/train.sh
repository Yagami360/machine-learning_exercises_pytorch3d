#!/bin/sh
#conda activate pytorch15_py36
set -eu
mkdir -p _logs

#----------------------
# model
#----------------------
N_EPOCHES=100
BATCH_SIZE=4

EXPER_NAME=debug
rm -rf tensorboard/${EXPER_NAME}
if [ ${EXPER_NAME} = "debug" ] ; then
    N_DISPLAY_STEP=10
else
    N_DISPLAY_STEP=100
fi

python train.py \
    --exper_name ${EXPER_NAME} \
    --n_epoches ${N_EPOCHES} \
    --n_diaplay_step ${N_DISPLAY_STEP} \
    --debug

if [ $1 = "poweroff" ] ; then
    sudo poweroff
    sudo shutdown -h now
fi
