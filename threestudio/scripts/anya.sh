#python launch.py --config configs/zero123.yaml --train --gpu 3 tag="max_step_percent=0.65-max_step=2000" system.renderer.num_samples_per_ray=384 data.random_camera.batch_size=6 system.material.textureless_prob=0.05 system.optimizer.args.lr=0.05 system.guidance.max_step_percent=0.65 system.loss.lambda_sparsity=2.0 system.material.ambient_only_steps=1 trainer.max_steps=2000

python launch.py --config configs/zero123.yaml --train --gpu 0 tag="anya_ph1"

# renamed as outputs/anya_ph1_latest

# claforte: doesn't work yet!
python launch.py --config configs/dreamfusion-if.yaml --train --gpu 0 \
  system.prompt_processor.prompt="A DSLR 3D photo of a cute anime schoolgirl stands proudly with her arms in the air, pink hair ( unreal engine 5 trending on Artstation Ghibli 4k )" \
  system.background.random_aug=true resume=outputs/zero123/anya_ph1_latest/ckpts/last.ckpt
