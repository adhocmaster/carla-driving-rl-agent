from core import learning
mode = "test"
town="Town03"
num_steps = 100
traffic = 'dense'
learning.evaluate(mode, town=town, steps=num_steps, seeds=[42], trials=10, traffic=traffic, weights='stage-s5-standard')