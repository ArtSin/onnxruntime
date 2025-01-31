# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation.  All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

import argparse
import os
import time

SD_MODELS = {
    "1.5": "runwayml/stable-diffusion-v1-5",
    "2.0": "stabilityai/stable-diffusion-2",
    "2.1": "stabilityai/stable-diffusion-2-1",
}


def example_prompts():
    prompts = [
        "a photo of an astronaut riding a horse on mars",
        "cute grey cat with blue eyes, wearing a bowtie, acrylic painting",
        "a cute magical flying dog, fantasy art drawn by disney concept artists, highly detailed, digital painting",
        "an illustration of a house with large barn with many cute flower pots and beautiful blue sky scenery",
        "one apple sitting on a table, still life, reflective, full color photograph, centered, close-up product",
        "background texture of stones, masterpiece, artistic, stunning photo, award winner photo",
        "new international organic style house, tropical surroundings, architecture, 8k, hdr",
        "beautiful Renaissance Revival Estate, Hobbit-House, detailed painting, warm colors, 8k, trending on Artstation",
        "blue owl, big green eyes, portrait, intricate metal design, unreal engine, octane render, realistic",
        "delicate elvish moonstone necklace on a velvet background, symmetrical intricate motifs, leaves, flowers, 8k",
    ]

    return prompts


def get_ort_pipeline(model_name: str, directory: str, provider: str, disable_safety_checker: bool):
    from diffusers import DPMSolverMultistepScheduler, OnnxStableDiffusionPipeline

    import onnxruntime

    if directory is not None:
        assert os.path.exists(directory)
        session_options = onnxruntime.SessionOptions()
        pipe = OnnxStableDiffusionPipeline.from_pretrained(
            directory,
            provider=provider,
            sess_options=session_options,
        )
    else:
        pipe = OnnxStableDiffusionPipeline.from_pretrained(
            model_name,
            revision="onnx",
            provider=provider,
            use_auth_token=True,
        )
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
    pipe.set_progress_bar_config(disable=True)

    if disable_safety_checker:
        pipe.safety_checker = None
        pipe.feature_extractor = None

    return pipe


def get_torch_pipeline(model_name: str, disable_safety_checker: bool):
    from diffusers import DPMSolverMultistepScheduler, StableDiffusionPipeline
    from torch import channels_last, float16

    pipe = StableDiffusionPipeline.from_pretrained(
        model_name, torch_dtype=float16, revision="fp16", use_auth_token=True
    ).to("cuda")
    pipe.unet.to(memory_format=channels_last)  # in-place operation
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
    pipe.set_progress_bar_config(disable=True)

    if disable_safety_checker:
        pipe.safety_checker = None
        pipe.feature_extractor = None

    return pipe


def get_image_filename_prefix(engine: str, model_name: str, batch_size: int, disable_safety_checker: bool):
    short_model_name = model_name.split("/")[-1].replace("stable-diffusion-", "sd")
    return f"{engine}_{short_model_name}_b{batch_size}" + ("" if disable_safety_checker else "_safe")


def run_ort_pipeline(pipe, batch_size: int, image_filename_prefix: str, height, width, steps, num_prompts, batch_count):
    from diffusers import OnnxStableDiffusionPipeline

    assert isinstance(pipe, OnnxStableDiffusionPipeline)

    prompts = example_prompts()

    pipe("warm up", height, width, num_inference_steps=steps)

    latency_list = []
    for i, prompt in enumerate(prompts):
        if i >= num_prompts:
            break
        for j in range(batch_count):
            inference_start = time.time()
            images = pipe(
                prompt,
                height,
                width,
                num_inference_steps=steps,
                negative_prompt=None,
                guidance_scale=7.5,
                num_images_per_prompt=batch_size,
            ).images
            inference_end = time.time()
            latency = inference_end - inference_start
            latency_list.append(latency)
            print(f"Inference took {latency} seconds")
            for k, image in enumerate(images):
                image.save(f"{image_filename_prefix}_{i}_{j}_{k}.jpg")

    print("Average latency in seconds:", sum(latency_list) / len(latency_list))


def run_torch_pipeline(
    pipe, batch_size: int, image_filename_prefix: str, height, width, steps, num_prompts, batch_count
):
    import torch

    prompts = example_prompts()

    pipe("warm up", height, width, num_inference_steps=steps)

    torch.set_grad_enabled(False)

    latency_list = []
    for i, prompt in enumerate(prompts):
        if i >= num_prompts:
            break
        torch.cuda.synchronize()
        for j in range(batch_count):
            inference_start = time.time()
            images = pipe(
                prompt=prompt,
                height=height,
                width=width,
                num_inference_steps=steps,
                guidance_scale=7.5,
                negative_prompt=None,
                num_images_per_prompt=batch_size,
                generator=None,  # torch.Generator
            ).images

            torch.cuda.synchronize()
            inference_end = time.time()
            latency = inference_end - inference_start
            latency_list.append(latency)
            print(f"Inference took {latency} seconds")
            for k, image in enumerate(images):
                image.save(f"{image_filename_prefix}_{i}_{j}_{k}.jpg")

    print("Average latency in seconds:", sum(latency_list) / len(latency_list))


def run_ort(
    model_name: str,
    directory: str,
    provider: str,
    batch_size: int,
    disable_safety_checker: bool,
    height,
    width,
    steps,
    num_prompts,
    batch_count,
):
    load_start = time.time()
    pipe = get_ort_pipeline(model_name, directory, provider, disable_safety_checker)
    load_end = time.time()
    print(f"Model loading took {load_end - load_start} seconds")

    image_filename_prefix = get_image_filename_prefix("ort", model_name, batch_size, disable_safety_checker)
    run_ort_pipeline(pipe, batch_size, image_filename_prefix, height, width, steps, num_prompts, batch_count)


def run_torch(
    model_name: str,
    batch_size: int,
    disable_safety_checker: bool,
    height,
    width,
    steps,
    num_prompts,
    batch_count,
):
    import torch

    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = True
    # torch.backends.cuda.matmul.allow_tf32 = True

    torch.set_grad_enabled(False)

    load_start = time.time()
    pipe = get_torch_pipeline(model_name, disable_safety_checker)
    load_end = time.time()
    print(f"Model loading took {load_end - load_start} seconds")

    image_filename_prefix = get_image_filename_prefix("torch", model_name, batch_size, disable_safety_checker)
    with torch.inference_mode():
        run_torch_pipeline(pipe, batch_size, image_filename_prefix, height, width, steps, num_prompts, batch_count)


def parse_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-e",
        "--engine",
        required=False,
        type=str,
        default="onnxruntime",
        choices=["onnxruntime", "torch"],
        help="Engines to benchmark. Default is onnxruntime.",
    )

    parser.add_argument(
        "-v",
        "--version",
        required=True,
        type=str,
        choices=list(SD_MODELS.keys()),
        help="Stable diffusion version like 1.5, 2.0 or 2.1",
    )

    parser.add_argument(
        "-p",
        "--pipeline",
        required=False,
        type=str,
        default=None,
        help="Directory of saved onnx pipeline. It could be output directory of optimize_pipeline.py.",
    )

    parser.add_argument(
        "--enable_safety_checker",
        required=False,
        action="store_true",
        help="Enable safety checker",
    )
    parser.set_defaults(enable_safety_checker=False)

    parser.add_argument(
        "-b",
        "--batch_size",
        type=int,
        default=1,
        choices=[1, 2, 4, 8, 16, 32],
        help="Number of images per batch",
    )

    parser.add_argument(
        "--height",
        required=False,
        type=int,
        default=512,
        help="Output image height",
    )

    parser.add_argument(
        "--width",
        required=False,
        type=int,
        default=512,
        help="Output image width",
    )

    parser.add_argument(
        "-s",
        "--steps",
        required=False,
        type=int,
        default=50,
        help="Number of steps",
    )

    parser.add_argument(
        "-n",
        "--num_prompts",
        required=False,
        type=int,
        default=1,
        help="Number of prompts",
    )

    parser.add_argument(
        "-c",
        "--batch_count",
        required=False,
        type=int,
        choices=range(1, 11),
        default=10,
        help="Number of batches to test",
    )

    args = parser.parse_args()
    return args


def main():
    args = parse_arguments()
    print(args)

    sd_model = SD_MODELS[args.version]
    if args.engine == "onnxruntime":
        assert args.pipeline, "--pipeline should be specified for onnxruntime engine"

        if args.batch_size > 1:
            # Need remove a line https://github.com/huggingface/diffusers/blob/a66f2baeb782e091dde4e1e6394e46f169e5ba58/src/diffusers/pipelines/stable_diffusion/pipeline_onnx_stable_diffusion.py#L307
            #    in diffuers to run batch_size > 1.
            assert (
                not args.enable_safety_checker
            ), "batch_size > 1 is not compatible with safety checker due to a bug in diffuers"

        provider = "CUDAExecutionProvider"  # TODO: use ["CUDAExecutionProvider", "CPUExecutionProvider"] in diffuers
        run_ort(
            sd_model,
            args.pipeline,
            provider,
            args.batch_size,
            not args.enable_safety_checker,
            args.height,
            args.width,
            args.steps,
            args.num_prompts,
            args.batch_count,
        )
    else:
        run_torch(
            sd_model,
            args.batch_size,
            not args.enable_safety_checker,
            args.height,
            args.width,
            args.steps,
            args.num_prompts,
            args.batch_count,
        )


if __name__ == "__main__":
    main()
