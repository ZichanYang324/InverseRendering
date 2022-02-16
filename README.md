# InverseRenderNet: Learning single image inverse rendering

## Evaluation

#### Dependencies
To run our evaluation code, please create your environment based on following dependencies:

    tensorflow 2.4.1
    python 3.8
    skimage
    cv2
    numpy

#### Pretrained model
* Download our pretrained model from: [Link](https://drive.google.com/file/d/1eV9IfdIgaPNXCRWw-DQea5eCX-807X1l/view?usp=sharing)
* Unzip the downloaded file 
* Make sure the model files are placed in a folder named "irn_model"


#### Test on demo image
You can perform inverse rendering on random RGB image by our pretrained model. To run the demo code, you need to specify the path to pretrained model, path to RGB image and corresponding mask which masked out sky in the image. Finally inverse rendering results will be saved to the output folder named by your argument.

```bash
python3 test_demo.py --model /PATH/TO/irn_model --image demo.jpg --mask demo_mask.jpg --output test_results
```







